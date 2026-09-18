"""Task 12's restart at the edges the journey cases leave open (TASKS.md
item 12).

``tests/test_restart.py`` walks TASKS.md item 12's two clauses on the middle of
the lifetime: a paste with an hour of its three left after the downtime is
served byte-for-byte on the link the first build returned, and a paste whose
deadline fell an hour inside the downtime gives the uniform 404 an unissued id
gets (PRD.md item 8). This module adds the instants and the restarts its chosen
numbers do not pin:

- tc1207: the downtime ends *exactly* on a paste's stored deadline. On the
  second build's first read that paste is the uniform 404 — so the boundary is
  strict and is crossed by the downtime rather than by any request — while a
  paste created one second later in the same first build, with one second of
  its three hours left, still comes back byte-for-byte on the link that build
  returned (PRD.md items 4, 5 and 8).
- tc1208: the restarted service is a continuation rather than only a reader of
  the past: a paste created through ``POST /pastes`` after the first restart is
  still served, byte-for-byte, by the build after the next restart, with the
  deadline the restarted build reported. A build that served from the file but
  wrote to somewhere the next build never opens would fail here (PRD.md item 8,
  ARCHITECTURE.md Journeys: Restart).
- tc1209: a paste that already survived one restart and was served in the
  second build still dies during a later downtime. Its row between the builds
  still carries the instant the first build wrote, so a read or a restart did
  not extend its three hours, and the third build answers its link with the
  uniform 404 (PRD.md item 4's "does not reset on read" and "service downtime
  or a restart does not extend it", item 8).

Every case builds the real ``app.main:app`` more than once inside one test —
each build its own lifespan over the same throwaway file, exactly as
``tests/test_restart.py`` does — and measures the file itself through a second
connection, so "the row is still there between the builds" and "the read
reclaimed it" are observations rather than inferences. The clock is the
fixture's ``FakeClock``, assigned between requests: no case waits, and the
sweeper's documented minute is in force throughout, so no tick can have run and
every answer has to come from the stored deadline (PRD.md Success,
ARCHITECTURE.md Sweep).
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

# PRD.md item 4's lifetime, written as the three hours the requirement states
# rather than taken from the module under test: every deadline below is derived
# from this number, and the case that leans on the boundary checks the constant
# against it (TASKS.md item 2).
THREE_HOURS_IN_SECONDS = 3 * 60 * 60
ONE_HOUR_SECONDS = 60 * 60
ONE_SECOND = 1.0

# The instant the cases create their first paste at, and the deadline that
# creation fixes (PRD.md item 4).
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + THREE_HOURS_IN_SECONDS

# The sweeper's documented period, which is in force while this module runs
# (nothing here shortens ``sweep_interval``): far longer than any case, so a
# row still stored between two builds is a measurement rather than a race
# (ARCHITECTURE.md Sweep).
DOCUMENTED_SWEEP_INTERVAL_SECONDS = 60

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# An id in the documented shape that was never issued, which every miss below
# must be indistinguishable from (PRD.md items 3 and 5).
UNKNOWN_ID = "Z" * 22

# The texts. Each carries the shapes PRD.md item 2 names, so an answer from a
# later build that re-encoded, trimmed or truncated a paste would be visible
# rather than coincidentally equal.
EXPIRED_TEXT = (
    "the paste whose deadline is the instant the service comes back\né ☃ 😀\n"
)
SURVIVOR_TEXT = "the paste with one second of its three hours left 😀\n"
BEFORE_TEXT = "  created before the first restart  \n\ttabbed 😀\n"
AFTER_TEXT = "created after the first restart, served after the second\né\n"
READ_BEFORE_TEXT = "read after one restart, dead after the next 😀\n"


@pytest.fixture(autouse=True)
def injected_clock(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """The fake clock, installed on the app for the whole case.

    The conftest ``client`` fixture holds one build of the app open for the
    length of a test, while a restart needs the app stopped and built again, so
    the cases here start their builds through ``_running_app`` and install the
    same two things themselves: the override key ``clock.now`` that the routes
    take, and the module-level ``clock.now`` that the lifespan's sweeper calls
    directly, so one instant decides both a read and a sweep with no real clock
    read (PRD.md Success). The override is cleared afterwards so a fake instant
    cannot leak into the next test.
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
    ``app.main:app`` entered as a context manager — store opened and sweeper
    started at startup, both stopped at shutdown, as the documented start
    command runs it (ARCHITECTURE.md Stack). That the build opened the
    throwaway file the ``db_path`` fixture set is asserted rather than assumed,
    so no build can reach the operator's database (PRD.md Success).
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

    Creating through the route keeps every case on the journey TASKS.md item 12
    names: the link opened in a later build is the one an earlier build
    returned, and the deadline the later build enforces is the one the create
    route stored (ARCHITECTURE.md Create).
    """
    fake_clock.instant = instant
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the builds opened,
    so "the row survived the stop", "the stored deadline did not move" and
    "the read reclaimed the row" are measured on the database rather than on
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
    (PRD.md applied defaults, ARCHITECTURE.md Create); that path is what every
    later build serves.
    """
    return urlsplit(paste["url"]).path


def _reported_deadline(paste: dict[str, str]) -> float:
    """The deadline the create response reported, as Unix seconds.

    PRD.md item 4 has the create response report the deadline as an instant, so
    tc1208 checks the restarted build's own deadline at the value that build
    told its creator rather than at a number the case recomputes
    (ARCHITECTURE.md Create).
    """
    return datetime.fromisoformat(paste["expires_at"]).timestamp()


def test_tc1207_a_downtime_ending_exactly_on_a_deadline_kills_it_and_spares_a_paste_a_second_inside(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """Items 4, 5 and 8: the restart instant is the deadline, and the boundary is strict."""
    assert config.PASTE_TTL_SECONDS == THREE_HOURS_IN_SECONDS
    assert config.SWEEP_INTERVAL_SECONDS == DOCUMENTED_SWEEP_INTERVAL_SECONDS

    # The second paste is created one second after the first, so its deadline
    # is the restart instant plus one second: exactly one second of its three
    # hours is left when the service comes back.
    survivor_created_at = CREATED_AT + ONE_SECOND
    survivor_deadline = DEADLINE + ONE_SECOND

    with _running_app(db_path) as first_build:
        expired = _created_paste(first_build, fake_clock, EXPIRED_TEXT, instant=CREATED_AT)
        survivor = _created_paste(
            first_build, fake_clock, SURVIVOR_TEXT, instant=survivor_created_at
        )

        # Both links work in the build that issued them, so the death below is
        # the restart's and the deadline's doing and not the create route's.
        for paste, text in ((expired, EXPIRED_TEXT), (survivor, SURVIVOR_TEXT)):
            alive = first_build.get(_paste_path(paste))
            assert alive.status_code == 200
            assert alive.content == text.encode("utf-8")

    rows_at_the_stop = sorted(
        [
            (expired["id"], EXPIRED_TEXT, CREATED_AT, DEADLINE),
            (survivor["id"], SURVIVOR_TEXT, survivor_created_at, survivor_deadline),
        ]
    )

    # The downtime ends exactly on the first paste's stored deadline: nothing
    # ran while the service was down, the documented sweeper period being far
    # longer than this case, so both rows are still in the file.
    assert sorted(_stored_rows(db_path)) == rows_at_the_stop

    fake_clock.instant = DEADLINE

    with _running_app(db_path) as second_build:
        assert sorted(_stored_rows(db_path)) == rows_at_the_stop

        # The deadline is in the past by the time the first read of the new
        # build arrives, and the rule is the one the read path applies
        # everywhere: retrievable strictly before the deadline, and the app's
        # one 404 from it on (PRD.md items 4 and 5).
        expired_now = second_build.get(_paste_path(expired))
        unknown = second_build.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

        assert expired_now.status_code == 404
        assert expired_now.content == unknown.content == NOT_FOUND_BODY
        assert expired_now.json() == {"error": NOT_FOUND_CODE}
        assert dict(expired_now.headers) == dict(unknown.headers)
        assert EXPIRED_TEXT.encode("utf-8") not in expired_now.content
        assert expired["id"] not in expired_now.text

        # One second of its three hours is still the paste's, so the restart
        # serves it on the link the first build returned, byte for byte
        # (PRD.md item 8).
        served = second_build.get(_paste_path(survivor))

        assert served.status_code == 200
        assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert served.content == SURVIVOR_TEXT.encode("utf-8")

        # The read reclaimed the dead row and only it: the paste with life left
        # keeps the instants the first build stored, neither extended by the
        # restart nor moved by being read (PRD.md item 4).
        assert _stored_rows(db_path) == [
            (survivor["id"], SURVIVOR_TEXT, survivor_created_at, survivor_deadline)
        ]


def test_tc1208_a_paste_created_after_a_restart_is_served_by_the_build_after_the_next_one(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 8: the restarted service writes to the one file every later build opens."""
    first_restart = CREATED_AT + ONE_HOUR_SECONDS
    before_deadline = CREATED_AT + THREE_HOURS_IN_SECONDS

    with _running_app(db_path) as first_build:
        before = _created_paste(first_build, fake_clock, BEFORE_TEXT, instant=CREATED_AT)
        assert first_build.get(_paste_path(before)).content == BEFORE_TEXT.encode("utf-8")

    # An hour of downtime: the first paste still has two hours of its three
    # when the service is back.
    fake_clock.instant = first_restart

    with _running_app(db_path) as second_build:
        # A paste created by the restarted process, with the instant that build
        # sees: its deadline is that instant plus the fixed lifetime (PRD.md
        # item 4), which is what tc1208's later build has to enforce.
        after = _created_paste(
            second_build, fake_clock, AFTER_TEXT, instant=first_restart
        )
        after_deadline = first_restart + THREE_HOURS_IN_SECONDS

        assert _reported_deadline(after) == after_deadline

        pre_restart = second_build.get(_paste_path(before))
        post_restart = second_build.get(_paste_path(after))

        assert pre_restart.status_code == 200
        assert pre_restart.content == BEFORE_TEXT.encode("utf-8")
        assert post_restart.status_code == 200
        assert post_restart.content == AFTER_TEXT.encode("utf-8")

    rows_after_the_second_build = sorted(
        [
            (before["id"], BEFORE_TEXT, CREATED_AT, before_deadline),
            (after["id"], AFTER_TEXT, first_restart, after_deadline),
        ]
    )

    # Both writes are in the file, the restarted build's included: a build that
    # kept its own insert anywhere but the one file would leave this short.
    assert sorted(_stored_rows(db_path)) == rows_after_the_second_build

    # A second downtime of another hour, so the first paste has an hour of its
    # three left and the paste created after the first restart has two.
    second_restart = first_restart + ONE_HOUR_SECONDS
    fake_clock.instant = second_restart

    with _running_app(db_path) as third_build:
        for paste, text in ((before, BEFORE_TEXT), (after, AFTER_TEXT)):
            served = third_build.get(_paste_path(paste))
            assert served.status_code == 200, paste["id"]
            assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
            assert served.content == text.encode("utf-8")

        # Serving a paste is not an expiry: both rows still carry the instants
        # their own build stored, so neither restart nor read moved a deadline
        # (PRD.md item 4).
        assert sorted(_stored_rows(db_path)) == rows_after_the_second_build


def test_tc1209_a_paste_served_after_one_restart_still_dies_during_a_later_downtime(
    db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md items 4 and 8: surviving one restart buys no extra life at the next one."""
    first_restart = CREATED_AT + ONE_HOUR_SECONDS
    second_restart = DEADLINE + ONE_HOUR_SECONDS

    with _running_app(db_path) as first_build:
        paste = _created_paste(
            first_build, fake_clock, READ_BEFORE_TEXT, instant=CREATED_AT
        )

    fake_clock.instant = first_restart

    with _running_app(db_path) as second_build:
        # The paste survives the first restart and is read there, which is the
        # read PRD.md item 4 says must not reset the lifetime.
        survived = second_build.get(_paste_path(paste))

        assert survived.status_code == 200
        assert survived.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert survived.content == READ_BEFORE_TEXT.encode("utf-8")

    rows_after_the_first_restart = [
        (paste["id"], READ_BEFORE_TEXT, CREATED_AT, DEADLINE)
    ]
    assert _stored_rows(db_path) == rows_after_the_first_restart

    # The second downtime runs from an hour before the paste's deadline to an
    # hour after it, and removes nothing by itself: the stop is the only thing
    # that happened to the file (ARCHITECTURE.md Journeys: Restart, PRD.md
    # item 6).
    fake_clock.instant = second_restart
    assert _stored_rows(db_path) == rows_after_the_first_restart

    with _running_app(db_path) as third_build:
        expired = third_build.get(_paste_path(paste))
        unknown = third_build.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

        # The instant the first build stored decides, not the restarts and not
        # the read in between: the link is now the same 404 an id that never
        # existed gets, with nothing of the text in it (PRD.md items 5 and 8).
        assert expired.status_code == 404
        assert expired.content == unknown.content == NOT_FOUND_BODY
        assert expired.json() == {"error": NOT_FOUND_CODE}
        assert dict(expired.headers) == dict(unknown.headers)
        assert READ_BEFORE_TEXT.encode("utf-8") not in expired.content
        assert paste["id"] not in expired.text

        # That read reclaimed the row, so the text is gone rather than hidden
        # (PRD.md item 6).
        assert _stored_rows(db_path) == []
