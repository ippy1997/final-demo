"""Task 9's reclamation at the edges its first cases do not pin (TASKS.md
item 9).

TASKS.md item 9 is one line: a sweeper task in the lifespan calling
``store.delete_expired(clock.now())`` every ``SWEEP_INTERVAL_SECONDS``, plus
deletion of an expired row on read, tested by the stored rows and text bytes
returning to their pre-paste level once the clock passes the deadlines.
``tests/test_reclaim.py`` covers the loop as a whole (tc901-tc906: the task
started and stopped, one sweep reclaiming several pastes, live pastes left
alone, the injected instant, a failed sweep, and a read reclaiming without the
sweeper). This module adds the cases those leave open, each named after the
acceptance case it proves:

- tc907: the sweeper by itself returns the table to the *empty* baseline —
  zero rows and zero stored bytes, measured through the live store's ``count``
  and ``text_bytes`` — with no read and no hand-written call, and a reclaimed
  link is the uniform 404 (PRD.md item 6, TASKS.md item 9).
- tc908: deadlines at different instants are honoured one crossing at a time:
  at the first deadline only the paste that reached it leaves, with the stored
  bytes falling by exactly its text, and only when the clock reaches the later
  deadline do rows and bytes return to the pre-paste baseline (PRD.md items 4
  and 6).
- tc909: the sweep's boundary is the stored deadline and it is inclusive — a
  tick that demonstrably ran one float step below the deadline leaves the row,
  its stored ``expires_at`` and its link untouched, and the tick at the
  deadline itself deletes it (PRD.md item 4's boundary default, item 6).
- tc910: every tick hands ``delete_expired`` the instant ``clock.now()`` holds
  at that tick: with the clock held still every recorded argument equals it,
  and after the clock moves the new instant appears, so no instant captured at
  startup is reused (AGENTS.md Conventions: the store reads no clock).
- tc911: the read path's deletion is the other half of the task and works on
  its own: with the documented minute in force and ``delete_expired`` forbidden
  outright, reading one of two expired pastes deletes exactly that row and its
  bytes, and reading the second returns the totals to the pre-paste baseline
  (ARCHITECTURE.md Read, PRD.md item 6).
- tc912: sweeps with every deadline still in the future delete nothing — the
  row set, the stored bytes and the stored deadline are unchanged after
  several ticks, and the link still serves the exact text (PRD.md item 4).
- tc913: a sweep that fails is reported with its traceback and does not end
  the loop: after two failures at the same instant the paste is still
  reclaimed by a later tick and the lifespan's task is not done
  (app/main.py, ``_sweep_expired_pastes``).
- tc916: an expired row nobody reads stays in the table while the sweeper's
  one call is blocked, so the sweeper is genuinely what reclaims it and no
  third mechanism does; the read then reclaims it and the table returns to its
  empty baseline (PRD.md item 6, ARCHITECTURE.md Read).

Every paste is created through ``POST /pastes`` and the clock is the fixture's
``FakeClock``, moved between requests, so no case waits three hours and none
reads the machine's real clock (PRD.md Success). The sweeper's period is the
fixture's ``sweep_interval``: the documented minute unless a case has to watch
a tick happen, in which case it asks for a short one indirectly, because the
lifespan's loop reads that constant when it takes each turn
(``tests/conftest.py``). The two things the plan does not decide — the real
sixty-second cadence between ticks and whether the database file shrinks on
disk — are recorded as untestable in the run's case list.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.store import Store

# The instants the cases create at and the deadline the create route derives
# from the first of them (PRD.md item 4).
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS
ONE_HOUR_SECONDS = 60 * 60

# The documented period of the sweeper, which the cases that have to watch a
# tick shorten through the fixture, and the shortened period itself: short
# enough that several ticks happen while a case waits, long enough that the
# loop is not spinning (ARCHITECTURE.md Sweep).
DOCUMENTED_SWEEP_INTERVAL_SECONDS = 60
FAST_SWEEP_INTERVAL_SECONDS = 0.01

# How long a case waits for an effect of the sweeper before calling it absent,
# and how often it looks: the sweeper runs in the app's own event loop, so its
# effect is the only thing a case can observe.
SWEEP_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.005

# The sweeper period, parametrized into ``sweep_interval`` for the cases that
# have to watch ticks happen (tests/conftest.py).
FAST_SWEEP = pytest.mark.parametrize(
    "sweep_interval", [FAST_SWEEP_INTERVAL_SECONDS], indirect=True
)

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# The texts: the live one carries the shapes PRD.md item 2 names so a
# truncated or re-encoded reclaimed row would stand out, and the doomed ones
# have different byte lengths so "the bytes fell by exactly this paste's text"
# is a real measurement rather than a count of rows.
LIVE_TEXT = "the paste that outlives every sweep — é ☃ 😀\n"
FIRST_DOOMED_TEXT = "the paste with the earlier deadline\n"
SECOND_DOOMED_TEXT = "the paste with the later deadline, and more bytes 😀😀\n"
MARKER_TEXT = "the tick marker: expired before the paste it watches\n"


class _ForbiddenSweep(AssertionError):
    """Raised when the sweeper ticks during a case that forbids it."""


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so reclamation is measured on the database rather than on an answer
    from the service (PRD.md item 6, TASKS.md item 9). WAL mode lets this read
    while the app's sweeper commits.
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _stored_text_bytes(rows: list[tuple[str, str, float, float]]) -> int:
    """The UTF-8 size of the text in ``rows``, the unit item 6 counts in."""
    return sum(len(text.encode("utf-8")) for _, text, _, _ in rows)


def _stored_ids(rows: list[tuple[str, str, float, float]]) -> set[str]:
    """The ids in ``rows``, for asking "has this paste left the table yet?"."""
    return {row[0] for row in rows}


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned."""
    return urlsplit(paste["url"]).path


def _created_paste(
    client: TestClient,
    fake_clock: FakeClock,
    text: str,
    instant: float = CREATED_AT,
) -> dict[str, str]:
    """Post ``text`` at ``instant`` and return the create response's body.

    Creating through the route rather than by hand keeps every case on the
    journey TASKS.md item 9 measures: the deadlines the sweeper works from are
    the ones the create route stored (ARCHITECTURE.md Create).
    """
    fake_clock.instant = instant
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def _wait_for(
    condition: Callable[[], bool], timeout: float = SWEEP_TIMEOUT_SECONDS
) -> bool:
    """Whether ``condition`` came true before ``timeout`` seconds had passed.

    The sweeper ticks in the app's own event loop, so a case observes it by
    waiting for its effect rather than by calling it; the wait ends as soon as
    the effect is there, and the condition is asked once more at the deadline
    so a slow machine is not reported as a missing sweep.
    """
    expiry = time.monotonic() + timeout
    while time.monotonic() < expiry:
        if condition():
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return condition()


def _record_sweeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Let every sweep run and record the instant it was handed.

    ``store.delete_expired`` is the sweeper's one call (ARCHITECTURE.md Sweep),
    so wrapping it is how a case sees the tick behind the effect without
    reaching into the task's loop.
    """
    real_delete_expired = Store.delete_expired
    instants: list[float] = []

    def recording(self: Store, now: float) -> int:
        instants.append(now)
        return real_delete_expired(self, now)

    monkeypatch.setattr(Store, "delete_expired", recording)
    return instants


def _block_sweeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every sweep and let it delete nothing, without failing the app.

    Used by the case that has to show an expired row stays put until a sweep
    succeeds: the tick is witnessed through the recorded instant and the row is
    left exactly as it is.
    """
    calls: list[float] = []

    def blocked(self: Store, now: float) -> int:
        calls.append(now)
        return 0

    monkeypatch.setattr(Store, "delete_expired", blocked)
    return calls


def _forbid_sweeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the sweeper's one call with a recorder that fails if it is made.

    Used where a tick must be impossible rather than merely harmless: the
    documented minute is in force, so a recorded call means the period was not
    respected and the case reports it.
    """
    calls: list[float] = []

    def forbidden(self: Store, now: float) -> int:
        calls.append(now)
        raise _ForbiddenSweep("the sweeper ran while this case needs the read alone")

    monkeypatch.setattr(Store, "delete_expired", forbidden)
    return calls


@FAST_SWEEP
def test_tc907_the_sweeper_by_itself_returns_rows_and_bytes_to_the_empty_baseline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 9: rows and text bytes return to their pre-paste level.

    No paste is read and the store is never called by hand: the table has to
    empty itself through the lifespan's task (PRD.md item 6). The baseline is
    the empty table the first paste was created on top of, measured through the
    live store's own counters rather than only through the file.
    """
    assert _stored_rows(db_path) == []

    doomed = [
        _created_paste(client, fake_clock, FIRST_DOOMED_TEXT),
        _created_paste(client, fake_clock, SECOND_DOOMED_TEXT),
    ]
    assert len(_stored_rows(db_path)) == 2
    assert _stored_text_bytes(_stored_rows(db_path)) == len(
        FIRST_DOOMED_TEXT.encode("utf-8")
    ) + len(SECOND_DOOMED_TEXT.encode("utf-8"))

    fake_clock.instant = DEADLINE

    assert _wait_for(lambda: _stored_rows(db_path) == [])

    assert _stored_rows(db_path) == []
    store = app.state.store
    assert store.count() == 0
    assert store.text_bytes() == 0

    # Reclamation and the answers agree: a reclaimed link is the uniform 404
    # an id that never existed gets (PRD.md item 5).
    reclaimed = client.get(_paste_path(doomed[0]))
    assert reclaimed.status_code == 404
    assert reclaimed.content == NOT_FOUND_BODY
    assert reclaimed.json() == {"error": NOT_FOUND_CODE}
    assert FIRST_DOOMED_TEXT not in reclaimed.text


@FAST_SWEEP
def test_tc908_each_deadline_is_reclaimed_as_the_clock_crosses_it(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Items 4 and 6: the stored deadline decides which paste a sweep collects.

    One sweep covers pastes with two different deadlines, so the case measures
    the crossing rather than the direction: at the earlier deadline the later
    paste is still stored, still counted in the text bytes and still served,
    and only when the clock reaches its own deadline does the table return to
    the level it had before either doomed paste was created.
    """
    live_created_at = CREATED_AT + 2 * ONE_HOUR_SECONDS
    later_created_at = CREATED_AT + ONE_HOUR_SECONDS
    live = _created_paste(client, fake_clock, LIVE_TEXT, instant=live_created_at)
    rows_before = _stored_rows(db_path)
    bytes_before = _stored_text_bytes(rows_before)
    assert rows_before == [
        (
            live["id"],
            LIVE_TEXT,
            live_created_at,
            live_created_at + config.PASTE_TTL_SECONDS,
        )
    ]

    earlier = _created_paste(client, fake_clock, FIRST_DOOMED_TEXT)
    later = _created_paste(
        client, fake_clock, SECOND_DOOMED_TEXT, instant=later_created_at
    )
    later_deadline = later_created_at + config.PASTE_TTL_SECONDS
    assert later_deadline == DEADLINE + ONE_HOUR_SECONDS

    fake_clock.instant = DEADLINE

    # Only a running sweeper can remove this row: nothing reads it.
    assert _wait_for(lambda: earlier["id"] not in _stored_ids(_stored_rows(db_path)))

    assert _stored_ids(_stored_rows(db_path)) == {live["id"], later["id"]}
    assert _stored_text_bytes(_stored_rows(db_path)) == bytes_before + len(
        SECOND_DOOMED_TEXT.encode("utf-8")
    )
    assert client.get(_paste_path(later)).status_code == 200
    assert client.get(_paste_path(earlier)).content == NOT_FOUND_BODY

    fake_clock.instant = later_deadline

    assert _wait_for(lambda: later["id"] not in _stored_ids(_stored_rows(db_path)))

    assert _stored_rows(db_path) == rows_before
    assert _stored_text_bytes(_stored_rows(db_path)) == bytes_before
    # The paste still inside its three hours is untouched, byte for byte.
    still_alive = client.get(_paste_path(live))
    assert still_alive.status_code == 200
    assert still_alive.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert still_alive.content == LIVE_TEXT.encode("utf-8")


@FAST_SWEEP
def test_tc909_the_sweep_deletes_at_the_deadline_and_not_one_step_early(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 4's boundary default, enforced by the sweeper as by the read.

    The marker paste's deadline is one second before the survivor's, so a tick
    that collects the marker proves a sweep ran at the instant under test:
    measured at the largest float below the survivor's deadline the survivor
    stays, row and deadline and link alike, and at the deadline itself it goes
    (ARCHITECTURE.md Sweep: "``expires_at <= clock.now()``").
    """
    marker = _created_paste(client, fake_clock, MARKER_TEXT, instant=CREATED_AT - 1)
    survivor = _created_paste(client, fake_clock, FIRST_DOOMED_TEXT)
    assert set(_stored_rows(db_path)) == {
        (marker["id"], MARKER_TEXT, CREATED_AT - 1, DEADLINE - 1),
        (survivor["id"], FIRST_DOOMED_TEXT, CREATED_AT, DEADLINE),
    }

    fake_clock.instant = math.nextafter(DEADLINE, -math.inf)

    assert _wait_for(lambda: marker["id"] not in _stored_ids(_stored_rows(db_path)))

    # A sweep has demonstrably run, and the paste short of its deadline is
    # still there with the deadline creation gave it.
    assert _stored_rows(db_path) == [
        (survivor["id"], FIRST_DOOMED_TEXT, CREATED_AT, DEADLINE)
    ]
    before = client.get(_paste_path(survivor))
    assert before.status_code == 200
    assert before.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert before.content == FIRST_DOOMED_TEXT.encode("utf-8")

    fake_clock.instant = DEADLINE

    assert _wait_for(lambda: survivor["id"] not in _stored_ids(_stored_rows(db_path)))

    assert _stored_rows(db_path) == []
    gone = client.get(_paste_path(survivor))
    assert gone.status_code == 404
    assert gone.content == NOT_FOUND_BODY
    assert FIRST_DOOMED_TEXT not in gone.text


@FAST_SWEEP
def test_tc910_every_tick_hands_delete_expired_the_current_injected_instant(
    client: TestClient, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASKS.md item 9: ``store.delete_expired(clock.now())``, every tick.

    The sweeper is the only caller, so every recorded argument is the instant
    the process's clock holds at that tick. Holding the clock still and then
    moving it shows both halves: the value is the injected one, and it is read
    per tick rather than captured when the task started (AGENTS.md
    Conventions: the store reads no clock of its own).
    """
    recorded = _record_sweeps(monkeypatch)
    first_instant = DEADLINE + 12.5
    fake_clock.instant = first_instant

    assert _wait_for(lambda: len(recorded) >= 2)

    assert len(recorded) >= 2
    assert set(recorded) == {first_instant}

    later_instant = first_instant + ONE_HOUR_SECONDS
    fake_clock.instant = later_instant

    assert _wait_for(lambda: later_instant in recorded)

    assert set(recorded) <= {first_instant, later_instant}


def test_tc911_a_read_reclaims_exactly_the_expired_paste_it_was_asked_for(
    client: TestClient,
    db_path: Path,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASKS.md item 9's other half: the read deletes the expired row itself.

    The sweeper's period is the documented minute and its one call is forbidden
    outright, so no tick can be the cause: whatever leaves the table is removed
    by the read that asked for it, one row at a time, and the rows and bytes
    return to the level they had before the expired pastes were created
    (ARCHITECTURE.md Read, PRD.md item 6).
    """
    assert config.SWEEP_INTERVAL_SECONDS == DOCUMENTED_SWEEP_INTERVAL_SECONDS
    sweeps = _forbid_sweeps(monkeypatch)

    live = _created_paste(
        client, fake_clock, LIVE_TEXT, instant=CREATED_AT + 2 * ONE_HOUR_SECONDS
    )
    rows_before = _stored_rows(db_path)
    bytes_before = _stored_text_bytes(rows_before)

    first = _created_paste(client, fake_clock, FIRST_DOOMED_TEXT)
    second = _created_paste(client, fake_clock, SECOND_DOOMED_TEXT)
    assert len(_stored_rows(db_path)) == len(rows_before) + 2

    fake_clock.instant = DEADLINE

    first_miss = client.get(_paste_path(first))

    assert first_miss.status_code == 404
    assert first_miss.content == NOT_FOUND_BODY
    assert first_miss.json() == {"error": NOT_FOUND_CODE}
    assert FIRST_DOOMED_TEXT not in first_miss.text
    assert _stored_ids(_stored_rows(db_path)) == {live["id"], second["id"]}
    assert _stored_text_bytes(_stored_rows(db_path)) == bytes_before + len(
        SECOND_DOOMED_TEXT.encode("utf-8")
    )

    second_miss = client.get(_paste_path(second))

    assert second_miss.status_code == 404
    assert second_miss.content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == rows_before
    assert _stored_text_bytes(_stored_rows(db_path)) == bytes_before
    # The read path's deletion did all of it; no sweep was ever made.
    assert sweeps == []
    assert client.get(_paste_path(live)).content == LIVE_TEXT.encode("utf-8")


@FAST_SWEEP
def test_tc912_sweeps_with_every_deadline_in_the_future_delete_nothing(
    client: TestClient,
    db_path: Path,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative half of item 6: reclamation must not touch live pastes.

    The clock stops one second short of the only deadline and several ticks are
    witnessed, so a sweep that deleted by age, by size or at startup would show
    here: the row, its bytes and its stored deadline are exactly what creation
    left, and the link still serves the text (PRD.md item 4, item 6).
    """
    recorded = _record_sweeps(monkeypatch)
    live = _created_paste(client, fake_clock, LIVE_TEXT)
    rows_before = _stored_rows(db_path)
    bytes_before = _stored_text_bytes(rows_before)
    assert rows_before == [(live["id"], LIVE_TEXT, CREATED_AT, DEADLINE)]

    fake_clock.instant = DEADLINE - 1

    assert _wait_for(lambda: len(recorded) >= 3)

    assert len(recorded) >= 3
    assert _stored_rows(db_path) == rows_before
    assert _stored_text_bytes(_stored_rows(db_path)) == bytes_before
    alive = client.get(_paste_path(live))
    assert alive.status_code == 200
    assert alive.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert alive.content == LIVE_TEXT.encode("utf-8")


@FAST_SWEEP
def test_tc913_a_failed_sweep_is_logged_with_its_error_and_the_task_survives(
    client: TestClient,
    db_path: Path,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store that fails a sweep must not take reclamation down with it.

    The first two sweeps after this point raise, as a locked database would;
    the paste still leaves the table, which is only possible if the loop
    carried on after each failure, and the failures are reported with their
    traceback because there is no caller to be told (app/main.py,
    ``_sweep_expired_pastes``; ARCHITECTURE.md Sweep).
    """
    doomed = _created_paste(client, fake_clock, FIRST_DOOMED_TEXT)

    real_delete_expired = Store.delete_expired
    attempted: list[float] = []

    def fail_the_first_two_sweeps(self: Store, now: float) -> int:
        attempted.append(now)
        if len(attempted) <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_delete_expired(self, now)

    monkeypatch.setattr(Store, "delete_expired", fail_the_first_two_sweeps)
    caplog.set_level(logging.ERROR)

    fake_clock.instant = DEADLINE

    assert _wait_for(lambda: _stored_rows(db_path) == [])

    assert len(attempted) >= 3
    error_records = [
        record for record in caplog.records if record.levelno >= logging.ERROR
    ]
    assert len(error_records) >= 2
    assert any(record.exc_info is not None for record in error_records)
    assert not app.state.sweeper.done()
    assert doomed["id"] not in _stored_ids(_stored_rows(db_path))


@FAST_SWEEP
def test_tc916_an_expired_row_stays_until_a_sweep_or_a_read_reclaims_it(
    client: TestClient,
    db_path: Path,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD.md item 6 with ARCHITECTURE.md Sweep: these two paths reclaim, no other.

    The sweeper's one call is blocked and no link has been opened, so the
    expired row is still in the table after several ticks of the loop: nothing
    else in the app removes it, which is exactly why item 6 needs the sweeper
    for a paste nobody reads again. The read then reclaims it and the table
    returns to the empty baseline it started from (ARCHITECTURE.md Read).
    """
    sweeps = _block_sweeps(monkeypatch)
    doomed = _created_paste(client, fake_clock, FIRST_DOOMED_TEXT)
    rows_at_creation = _stored_rows(db_path)
    assert rows_at_creation == [
        (doomed["id"], FIRST_DOOMED_TEXT, CREATED_AT, DEADLINE)
    ]

    fake_clock.instant = DEADLINE

    assert _wait_for(lambda: len(sweeps) >= 2)

    # Ticks happened and reclaimed nothing: with the sweeper out of the way and
    # no read made, the expired text is still stored.
    assert len(sweeps) >= 2
    assert _stored_rows(db_path) == rows_at_creation
    assert _stored_text_bytes(_stored_rows(db_path)) == len(
        FIRST_DOOMED_TEXT.encode("utf-8")
    )

    response = client.get(_paste_path(doomed))

    assert response.status_code == 404
    assert response.content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == []
    assert _stored_text_bytes(_stored_rows(db_path)) == 0
