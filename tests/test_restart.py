"""Task 12: the restart journey — two builds of the app over one database file
(TASKS.md item 12).

TASKS.md item 12 is one line: build the app twice against one database file,
assert that a paste still inside its three hours resolves and that a paste
which reached its deadline during the downtime gives the uniform 404. PRD.md
item 8 is what it serves — "unexpired pastes survive a restart": after the
service is stopped and started again, a link created before the restart and
still inside its three hours returns its text, and one that reached its
deadline during the downtime returns the same 404 as an id that never existed.
ARCHITECTURE.md's Restart journey fixes how that works: the deadline is a
stored instant (its Data section and its decision row "``expires_at`` stored as
an absolute instant"), so startup reopens the same file, an unexpired paste is
still there with the instant it was created with, and a paste that died while
the process was down is swept or 404s on read. Downtime counts against the
three hours rather than pausing them (PRD.md item 4 and its applied default),
which is why the second build enforces the deadline the first one stored
rather than a fresh three hours from the restart.

Only the app's own file makes that true: there is no second service, cache or
queue to replay and nothing is held in the process (ARCHITECTURE.md Stack). The
cases here therefore build the real ``app.main:app`` more than once inside one
test — its lifespan opening the same throwaway file each time, as the
documented start command does — and measure both the answers and the file the
builds shared:

- tc1201: the journey in one case. A paste created before the stop and still
  inside its three hours is served byte-for-byte on the link the first build
  returned, after the second build has started; a paste whose deadline fell
  during the downtime answers the uniform 404 on its own link, and its row is
  the one that read reclaims (PRD.md item 8, items 4 and 6). The first build is
  proved stopped rather than left running beside the second.
- tc1202: the 404 a paste that died during the downtime gives is the app's one
  404: same status, body and headers as an id that was never issued, a
  malformed id and a path no route matches, with nothing of the text and no
  header that hints the paste ever existed (PRD.md item 5).
- tc1203: the downtime deletes nothing by itself. Between the two builds both
  rows are still in the file with the instants creation gave them — no sweep
  can have run, the documented minute being far longer than a test — and it is
  the first read after the restart that reclaims the dead one, leaving the live
  one alone (PRD.md items 4 and 6).
- tc1204: the restart does not extend a paste's three hours. The deadline the
  create response reported before the downtime is the instant the second build
  stops serving at, which is what rules out a deadline recomputed from the
  restart instant (PRD.md items 4 and 8).
- tc1205: a second restart, after the dead paste was read and its row
  reclaimed, still serves the live paste and still answers that link with the
  uniform 404: survival and death both come from the file, not from what a
  process happened to hold (PRD.md items 5 and 8).
- tc1206: the second build decides both answers on the injected clock and never
  the machine's, with ``clock.time`` forbidden across the stop and the restart,
  so the downtime is crossed without waiting (PRD.md Success).

Every paste is created through ``POST /pastes`` in one build and opened through
the link that build returned, in the next. ``tests/test_store.py`` already pins
the store's own reopening on one file (its
``test_reopening_the_file_after_a_deadline_passed_still_reads_the_row`` is the
row-level shape of tc1203) and ``tests/test_reclaim.py`` the sweeper; what this
module adds is the service itself stopped and started again. That is why it
does not use the conftest ``client`` fixture: that fixture holds one build of
the app open for the length of a test, while a restart needs the app closed and
built again. What that fixture installs is installed here instead — the same
``clock.now`` override and the same patch of the sweeper's clock — and every
build is required to have opened the throwaway file the ``db_path`` fixture
set, so this module still never reaches the operator's database (PRD.md
Success). No case waits for a sweep: the documented
``config.SWEEP_INTERVAL_SECONDS`` is in force throughout, which is what makes
"the dead row is still in the file" a measurement rather than a race.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import clock, config
from app.main import app

# The instant the paste that dies during the downtime is created at, and the
# deadline the create route derives from it (PRD.md item 4). That deadline is
# inside the downtime below, which is the first of the task's two clauses
# (TASKS.md item 12, PRD.md item 8).
CREATED_AT = 1_700_000_000.0
ONE_HOUR_SECONDS = 60 * 60
DIED_DURING_DOWNTIME_DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The instant the paste that outlives the downtime is created at — two hours
# after the first, which is the last request the first build serves — and the
# deadline it carries. An hour of its three is left when the service is back,
# so the second clause of the task is measured on a paste genuinely inside its
# lifetime rather than on the edge of it.
SURVIVOR_CREATED_AT = CREATED_AT + 2 * ONE_HOUR_SECONDS
SURVIVOR_DEADLINE = SURVIVOR_CREATED_AT + config.PASTE_TTL_SECONDS

# The instant the restarted build sees: two hours of downtime, which spans the
# first paste's deadline (an hour behind it) and leaves the second paste an
# hour of its three (PRD.md items 4 and 8).
RESTARTED_AT = SURVIVOR_CREATED_AT + 2 * ONE_HOUR_SECONDS

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# The sweeper's period while these cases run: the documented minute
# (app/config.py), far longer than any case, so no tick can happen inside one
# and "the dead row is still stored" is a measurement rather than a race
# (ARCHITECTURE.md Sweep).
DOCUMENTED_SWEEP_INTERVAL_SECONDS = 60

# The ways a read misses that the dead paste's answer must be
# indistinguishable from (PRD.md item 5): an id in the documented shape that
# was never issued, an id that is not in that shape at all, and a path no route
# matches (PRD.md item 3, ARCHITECTURE.md Error contract).
UNKNOWN_ID = "Z" * 22
MALFORMED_ID = "no-such-id"
UNMATCHED_PATH = "/nothing-here"

# The headers that would tell a reader "this paste expired" rather than "this
# id never existed" if the answer carried one (PRD.md item 5).
HINT_HEADERS = ("retry-after", "www-authenticate", "location")

# The two texts the journey is walked with. Both carry the shapes PRD.md item 2
# names, so an answer from the restarted build that re-encoded, trimmed or
# truncated a paste would be visible rather than coincidentally equal.
DIED_TEXT = (
    "the paste whose three hours ended while the service was down\n"
    "second line\té ☃ 😀\n"
)
SURVIVOR_TEXT = (
    "  the paste that is still inside its three hours after the restart  \n"
    "\ttabbed 😀\n"
)


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant.

    Installed in place of ``clock.time`` to prove the restarted build decides
    both of its answers from the injected clock and never from the machine's:
    an expiry that asked the machine for the time would raise here instead of
    answering (PRD.md items 4 and Success).
    """

    def time(self) -> float:
        raise AssertionError("the real clock was read while a fake one was set")


@pytest.fixture(autouse=True)
def injected_clock(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """The fake clock, installed on the app for the whole case.

    The conftest ``client`` fixture installs exactly these two things, but it
    also holds one build of the app open for the length of the test; a restart
    needs the app stopped and built again, so each case here starts its builds
    through ``_running_app`` and the install is done once, here, around all of
    them. ``clock.now`` is replaced in the module as well as in
    ``app.dependency_overrides`` because the lifespan's sweeper calls that
    function directly rather than through a request dependency, so one instant
    decides both a read and a sweep with no real clock read (PRD.md Success).
    The override is cleared afterwards, so a fake instant cannot leak into the
    next test.
    """
    app.dependency_overrides[clock.now] = fake_clock.now
    monkeypatch.setattr(clock, "now", fake_clock.now)
    try:
        yield
    finally:
        app.dependency_overrides.clear()


@contextmanager
def _running_app(db_path: Path) -> Iterator[TestClient]:
    """One build of the app: its lifespan started, then stopped, on one file.

    Every build a case makes goes through here, so each one is the real
    ``app.main:app`` entered as a context manager — the store opened and the
    sweeper task started at startup, both stopped at shutdown, exactly as the
    documented start command runs it (ARCHITECTURE.md Stack, TASKS.md item 9).
    That the build opened the throwaway file the ``db_path`` fixture set is
    asserted rather than assumed, so "twice against one database file" is
    measured on the builds themselves and no build can reach the operator's
    database (PRD.md Success).
    """
    with TestClient(app) as running:
        assert app.state.store.path == db_path
        yield running


def _created_paste(
    client: TestClient,
    fake_clock: FakeClock,
    text: str,
    instant: float,
) -> dict[str, str]:
    """Post ``text`` at ``instant`` and return the create response's body.

    Creating through the route rather than inserting by hand keeps every case
    on the journey TASKS.md item 12 names: the link opened after the restart is
    the one the first build returned, and the deadline the second build
    enforces is the one the create route stored before it (ARCHITECTURE.md
    Create).
    """
    fake_clock.instant = instant
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the builds opened,
    so "both rows survived the stop", "the stored deadline did not move" and
    "the dead row was reclaimed" are measured on the database rather than on
    the answers (PRD.md items 4, 6 and 8). WAL mode lets this read while the
    app may write.
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned.

    The url is absolute and built from the request's own host, so each case
    opens the path a browser would open rather than rebuilding one from the id
    (PRD.md applied defaults, ARCHITECTURE.md Create); the path survives the
    restart because the second build serves the same routes.
    """
    return urlsplit(paste["url"]).path


def _reported_deadline(paste: dict[str, str]) -> float:
    """The deadline the create response reported, as Unix seconds.

    PRD.md item 4 has the create response report the deadline as an instant, so
    tc1204 probes the second build's boundary at the instant the first build
    told the creator about rather than at a number the case recomputes
    (ARCHITECTURE.md Create).
    """
    return datetime.fromisoformat(paste["expires_at"]).timestamp()


def test_tc1201_a_paste_inside_its_three_hours_resolves_after_a_restart_and_a_dead_one_does_not(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 12's two clauses, on one file and two builds (PRD.md item 8)."""
    with _running_app(db_path) as first_build:
        died = _created_paste(first_build, fake_clock, DIED_TEXT, instant=CREATED_AT)
        survivor = _created_paste(
            first_build, fake_clock, SURVIVOR_TEXT, instant=SURVIVOR_CREATED_AT
        )
        first_store = app.state.store

        # Both links work in the build that issued them, so what the second
        # build answers below is the restart's doing and not the create
        # route's.
        alive_before = first_build.get(_paste_path(survivor))
        assert alive_before.status_code == 200
        assert alive_before.content == SURVIVOR_TEXT.encode("utf-8")

    # The first build is stopped before the second starts: the lifespan closed
    # its one connection, so it can serve nothing while the clock crosses the
    # downtime (app/main.py, lifespan).
    with pytest.raises(sqlite3.ProgrammingError):
        first_store.count()

    # The downtime: the clock moves from the survivor's creation instant — the
    # last request the first build served — to four hours after the first
    # paste's creation, which is past that paste's deadline and an hour short
    # of the survivor's.
    fake_clock.instant = RESTARTED_AT

    with _running_app(db_path) as second_build:
        # A second build, not the first one kept open: the lifespan opened a
        # new store on the same file.
        assert app.state.store is not first_store

        # Survival comes from the file: both rows are still there with the
        # instants creation gave them, so the second build reads the first
        # build's work and no deadline was recomputed by the restart (PRD.md
        # item 4).
        assert sorted(_stored_rows(db_path)) == sorted(
            [
                (died["id"], DIED_TEXT, CREATED_AT, DIED_DURING_DOWNTIME_DEADLINE),
                (
                    survivor["id"],
                    SURVIVOR_TEXT,
                    SURVIVOR_CREATED_AT,
                    SURVIVOR_DEADLINE,
                ),
            ]
        )

        # The link returned before the restart serves the text byte-for-byte
        # after it (PRD.md item 8, item 2).
        served = second_build.get(_paste_path(survivor))

        assert served.status_code == 200
        assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert served.content == SURVIVOR_TEXT.encode("utf-8")

        # The link whose deadline fell during the downtime is the uniform 404,
        # the same answer an id that never existed gets (PRD.md item 8, item 5).
        expired = second_build.get(_paste_path(died))
        unknown = second_build.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

        assert expired.status_code == 404
        assert expired.content == NOT_FOUND_BODY
        assert expired.json() == {"error": NOT_FOUND_CODE}
        assert expired.content == unknown.content
        assert dict(expired.headers) == dict(unknown.headers)
        assert DIED_TEXT.encode("utf-8") not in expired.content
        assert died["id"] not in expired.text

        # That read reclaimed the dead row, while the paste still inside its
        # three hours keeps its row and its stored instants: the two clauses of
        # the task are decided by the stored deadline and by nothing else
        # (PRD.md items 4 and 6).
        assert _stored_rows(db_path) == [
            (
                survivor["id"],
                SURVIVOR_TEXT,
                SURVIVOR_CREATED_AT,
                SURVIVOR_DEADLINE,
            )
        ]


def test_tc1202_the_404_after_the_restart_is_the_apps_one_404(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 5: a restart must not give the dead paste's answer a tell."""
    with _running_app(db_path) as first_build:
        died = _created_paste(first_build, fake_clock, DIED_TEXT, instant=CREATED_AT)
        _created_paste(
            first_build, fake_clock, SURVIVOR_TEXT, instant=SURVIVOR_CREATED_AT
        )

    fake_clock.instant = RESTARTED_AT

    with _running_app(db_path) as second_build:
        responses = {
            "paste that died during the downtime": second_build.get(_paste_path(died)),
            "made-up id": second_build.get(f"{PASTES_PATH}/{UNKNOWN_ID}"),
            "malformed id": second_build.get(f"{PASTES_PATH}/{MALFORMED_ID}"),
            "unmatched path": second_build.get(UNMATCHED_PATH),
        }

        reference = responses["made-up id"]
        assert reference.status_code == 404
        assert reference.content == NOT_FOUND_BODY
        assert reference.json() == {"error": NOT_FOUND_CODE}

        # One status, one body, the same headers, whoever asks and whatever the
        # id used to be: the restart changes nothing about the answer (PRD.md
        # item 5).
        for case, response in responses.items():
            assert response.status_code == reference.status_code, case
            assert response.content == reference.content, case
            assert dict(response.headers) == dict(reference.headers), case

        dead = responses["paste that died during the downtime"]
        assert DIED_TEXT.encode("utf-8") not in dead.content
        assert died["id"] not in dead.text
        for header in HINT_HEADERS:
            assert header not in dead.headers, header


def test_tc1203_the_downtime_deletes_nothing_and_the_first_read_after_it_reclaims_the_row(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """Items 4 and 6: a stopped service has no sweeper, so the read reclaims the row."""
    with _running_app(db_path) as first_build:
        died = _created_paste(first_build, fake_clock, DIED_TEXT, instant=CREATED_AT)
        survivor = _created_paste(
            first_build, fake_clock, SURVIVOR_TEXT, instant=SURVIVOR_CREATED_AT
        )

    rows_at_the_stop = sorted(
        [
            (died["id"], DIED_TEXT, CREATED_AT, DIED_DURING_DOWNTIME_DEADLINE),
            (survivor["id"], SURVIVOR_TEXT, SURVIVOR_CREATED_AT, SURVIVOR_DEADLINE),
        ]
    )

    # Between the two builds, with the first paste's deadline already behind
    # it, both rows are still in the file: the downtime removes nothing,
    # because nothing is running to remove it (ARCHITECTURE.md Journeys:
    # Restart).
    assert config.SWEEP_INTERVAL_SECONDS == DOCUMENTED_SWEEP_INTERVAL_SECONDS
    assert sorted(_stored_rows(db_path)) == rows_at_the_stop

    fake_clock.instant = RESTARTED_AT

    with _running_app(db_path) as second_build:
        # Startup starts the sweeper but does not sweep: its first tick is one
        # interval away, the interval is the documented minute and no case
        # waits that long, so the row the read below removes is still the first
        # build's row (app/main.py, ``_sweep_expired_pastes``).
        assert sorted(_stored_rows(db_path)) == rows_at_the_stop

        expired = second_build.get(_paste_path(died))

        assert expired.status_code == 404
        assert expired.content == NOT_FOUND_BODY
        assert DIED_TEXT.encode("utf-8") not in expired.content

        # The read reclaimed the dead row and nothing else: the paste inside
        # its three hours is still stored with its instants, and still served
        # on its link (PRD.md items 4, 5 and 6).
        assert _stored_rows(db_path) == [
            (
                survivor["id"],
                SURVIVOR_TEXT,
                SURVIVOR_CREATED_AT,
                SURVIVOR_DEADLINE,
            )
        ]
        served = second_build.get(_paste_path(survivor))
        assert served.status_code == 200
        assert served.content == SURVIVOR_TEXT.encode("utf-8")


def test_tc1204_the_restart_does_not_extend_the_three_hours_the_first_build_stored(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md items 4 and 8: downtime counts against the lifetime, it does not pause it."""
    with _running_app(db_path) as first_build:
        survivor = _created_paste(
            first_build, fake_clock, SURVIVOR_TEXT, instant=SURVIVOR_CREATED_AT
        )

        # The instant the creator was told about before the downtime, which is
        # the one the restarted build has to enforce.
        reported = _reported_deadline(survivor)
        assert reported == SURVIVOR_DEADLINE

    fake_clock.instant = RESTARTED_AT

    with _running_app(db_path) as second_build:
        fake_clock.instant = reported - 1
        before = second_build.get(_paste_path(survivor))

        assert before.status_code == 200
        assert before.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert before.content == SURVIVOR_TEXT.encode("utf-8")

        # Serving the text wrote nothing: the deadline the first build stored
        # is still the row's, so the restarted build neither moved it forward
        # nor handed the paste a fresh three hours (PRD.md item 4).
        assert _stored_rows(db_path) == [
            (
                survivor["id"],
                SURVIVOR_TEXT,
                SURVIVOR_CREATED_AT,
                SURVIVOR_DEADLINE,
            )
        ]

        # At that reported instant the paste is gone — which is what rules out
        # a deadline recomputed from the restart: a fresh three hours from
        # RESTARTED_AT would still be serving here, an hour later (PRD.md
        # items 4 and 8).
        fake_clock.instant = reported
        at = second_build.get(_paste_path(survivor))
        unknown = second_build.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

        assert at.status_code == 404
        assert at.content == unknown.content == NOT_FOUND_BODY
        assert dict(at.headers) == dict(unknown.headers)
        assert SURVIVOR_TEXT.encode("utf-8") not in at.content
        assert _stored_rows(db_path) == []


def test_tc1205_a_second_restart_keeps_the_live_paste_served_and_the_reclaimed_one_dead(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md items 5 and 8: both answers come from the file, not from process memory."""
    with _running_app(db_path) as first_build:
        died = _created_paste(first_build, fake_clock, DIED_TEXT, instant=CREATED_AT)
        survivor = _created_paste(
            first_build, fake_clock, SURVIVOR_TEXT, instant=SURVIVOR_CREATED_AT
        )

    fake_clock.instant = RESTARTED_AT

    with _running_app(db_path) as second_build:
        assert second_build.get(_paste_path(died)).content == NOT_FOUND_BODY

    # The read in the second build reclaimed the dead row, so the third build
    # finds one paste in the file and answers from that alone.
    assert _stored_rows(db_path) == [
        (survivor["id"], SURVIVOR_TEXT, SURVIVOR_CREATED_AT, SURVIVOR_DEADLINE)
    ]

    fake_clock.instant = RESTARTED_AT + ONE_HOUR_SECONDS // 2

    with _running_app(db_path) as third_build:
        served = third_build.get(_paste_path(survivor))
        reclaimed = third_build.get(_paste_path(died))
        unknown = third_build.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

        assert served.status_code == 200
        assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert served.content == SURVIVOR_TEXT.encode("utf-8")

        # The id that was reclaimed is not resurrected by another restart, and
        # its answer is still the one an id that never existed gets (PRD.md
        # item 5).
        assert reclaimed.status_code == 404
        assert reclaimed.content == unknown.content == NOT_FOUND_BODY
        assert dict(reclaimed.headers) == dict(unknown.headers)
        assert DIED_TEXT.encode("utf-8") not in reclaimed.content


def test_tc1206_the_second_build_answers_on_the_injected_clock_and_not_the_real_one(
    db_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD.md Success: the downtime is crossed by moving the clock, never by waiting."""
    with _running_app(db_path) as first_build:
        died = _created_paste(first_build, fake_clock, DIED_TEXT, instant=CREATED_AT)
        survivor = _created_paste(
            first_build, fake_clock, SURVIVOR_TEXT, instant=SURVIVOR_CREATED_AT
        )

    # Forbidden for the whole restart: a build or a read that asked the machine
    # for the time would raise here instead of answering (PRD.md item 4).
    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    fake_clock.instant = RESTARTED_AT

    with _running_app(db_path) as second_build:
        served = second_build.get(_paste_path(survivor))
        expired = second_build.get(_paste_path(died))
        unknown = second_build.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert served.status_code == 200
    assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert served.content == SURVIVOR_TEXT.encode("utf-8")

    assert expired.status_code == 404
    assert expired.content == unknown.content == NOT_FOUND_BODY
    assert dict(expired.headers) == dict(unknown.headers)
    assert DIED_TEXT.encode("utf-8") not in expired.content
