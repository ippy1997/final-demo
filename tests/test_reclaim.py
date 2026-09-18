"""Task 9: reclamation by the lifespan's sweeper and by the read path
(TASKS.md item 9).

TASKS.md item 9 is one line: a sweeper task in the lifespan calling
``store.delete_expired(clock.now())`` every ``SWEEP_INTERVAL_SECONDS``, plus
deletion of an expired row on read, tested by the stored rows and text bytes
returning to their pre-paste level once the clock passes the deadlines. PRD.md
item 6 is what that serves — expired text is reclaimed rather than only hidden
from a reader, so a long-running service does not accumulate every paste ever
made — and ARCHITECTURE.md fixes the shape: one asyncio task in the same
process, the period in ``app/config.py``, and the measurement in
ARCHITECTURE.md's Data section, rows and the UTF-8 bytes of the stored text in
``pastes``.

What the cases below pin:

- tc901: the sweeper is one task the lifespan starts and stops — running while
  the app serves, cancelled and awaited before the store is closed, so no task
  outlives the connection it uses.
- tc902: with the clock past the deadlines, the sweeper reclaims by itself —
  nothing reads the pastes and nothing calls the store by hand — and the row
  count and the stored text bytes return to what they were before those pastes
  were created (PRD.md item 6, TASKS.md item 9).
- tc903: the sweeper's boundary is the read path's boundary: a tick that has
  demonstrably run — watched through the paste it collected — leaves a paste
  still inside its three hours stored, byte for byte, and still served on its
  link, tick after tick.
- tc904: the sweeper's instant comes from ``clock.now()`` and not from the
  machine's clock: the injected instant is held while the real clock is
  forbidden, and the sweeper still collects exactly the pastes whose deadline
  has passed on that injected instant (PRD.md Success).
- tc905: a sweep that fails is reported and does not end the task: the row is
  still reclaimed by a later tick, so a database that was briefly locked does
  not stop reclamation for the life of the process.
- tc906: a read of an expired paste reclaims it on its own, with the store
  returning to its earlier level, which is the other half of TASKS.md item 9
  and the reason the row is deleted rather than filtered (ARCHITECTURE.md
  Read).

Every paste is created through ``POST /pastes`` and the clock is the fixture's
``FakeClock``, moved between requests: no case waits three hours and none
depends on the machine's real clock (PRD.md Success). What a case does wait for
is the sweeper, whose period is the fixture's ``sweep_interval`` — the
documented minute by default, shortened indirectly by the cases that have to
watch a tick happen, because the lifespan reads that constant when it runs.
``tests/test_store.py`` and ``tests/test_store_contract.py`` already call
``store.delete_expired`` directly; what this module adds is the task that calls
it, which is why it observes the store through the running app instead.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import clock, config
from app.main import app
from app.store import Store

# The instant a paste is created at here, the deadline the create route derives
# from it, and the step the cases use to give two pastes different deadlines
# (PRD.md item 4).
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS
ONE_HOUR_SECONDS = 60 * 60

# The documented period of the sweeper, which the cases that have to wait for
# the sweeper to have run inside one test shorten (ARCHITECTURE.md Sweep).
DOCUMENTED_SWEEP_INTERVAL_SECONDS = 60

# The period the sweeper runs at in those cases: short enough that several
# ticks happen while a test waits, long enough that the loop is not spinning.
FAST_SWEEP_INTERVAL_SECONDS = 0.01

# How long a case waits for an effect of the sweeper before calling it absent,
# and how often it looks. The sweeper runs in the app's own event loop, so its
# effect is the only thing a case can observe.
SWEEP_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.005

# How many further ticks a case lets pass while it checks that a paste with
# time left is still stored.
TICKS_OBSERVED = 5

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# The instant the pastes with time left are created at, so their deadline is
# an hour past the deadline of the pastes created at CREATED_AT.
LATER_CREATED_AT = CREATED_AT + ONE_HOUR_SECONDS
LATER_DEADLINE = LATER_CREATED_AT + config.PASTE_TTL_SECONDS

# The texts, carrying the shapes PRD.md item 2 names, so a reclaimed row's
# bytes are recognisable and a truncated one would stand out.
KEPT_TEXT = "the paste that is still inside its three hours — é ☃ 😀\n"
LIVE_TEXT = "the paste the sweeper must leave alone\nsecond line\t😀\n"
DOOMED_TEXT = "the paste nobody reads again\nsecond line\t😀\n"
DOOMED_TEXTS = [
    "the first paste to expire\n",
    "the second paste to expire — é ☃\n",
    "the third paste to expire 😀\n",
]

# The sweeper period, parametrized into ``sweep_interval`` for the cases that
# have to watch a tick. ``client`` installs the value into ``config`` before
# the app is started (tests/conftest.py).
FAST_SWEEP = pytest.mark.parametrize(
    "sweep_interval", [FAST_SWEEP_INTERVAL_SECONDS], indirect=True
)


class _ClockReadForbidden(AssertionError):
    """Raised when the real clock is read while a fake one is installed."""


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant.

    Installed in place of ``clock.time`` to prove the sweeper's instant is the
    injected one: a sweep that asked the machine for the time would raise here
    instead of reclaiming anything (PRD.md Success).
    """

    def time(self) -> float:
        raise _ClockReadForbidden("the real clock was read while a fake one was set")


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
    """The UTF-8 size of the text in ``rows``, the other half of item 6's measure."""
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

    Creating through the route rather than by hand keeps the case on the
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


def test_tc901_the_lifespan_starts_one_sweeper_task_and_stops_it_at_shutdown(
    db_path: Path,
) -> None:
    """TASKS.md item 9: the sweeper belongs to the lifespan, not to a request.

    The task is started with the store and cancelled before the store is
    closed, so nothing is left running against a closed connection when the
    process shuts down (PRD.md item 8).
    """
    with TestClient(app) as started:
        sweeper = app.state.sweeper

        assert isinstance(sweeper, asyncio.Task)
        assert not sweeper.done()
        # The lifespan opened the throwaway file, not the operator's database.
        assert app.state.store.path == db_path
        # The app serves while the sweeper is up: the task is not startup work
        # that has to finish before the first request is answered.
        assert started.get("/health").status_code == 200

    assert sweeper.done()
    assert sweeper.cancelled()


@FAST_SWEEP
def test_tc902_the_sweeper_reclaims_the_rows_and_the_text_bytes_of_expired_pastes(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 9's measurement: rows and bytes return to their earlier level.

    Nothing reads the expired pastes and nothing calls the store by hand: the
    reclamation has to come from the lifespan's task (PRD.md item 6). The
    paste created an hour later is the level the measurement returns to, and it
    is also the control: its deadline has not passed, so a sweep that took
    everything would fail the case.
    """
    kept = _created_paste(client, fake_clock, KEPT_TEXT, instant=LATER_CREATED_AT)
    rows_before = _stored_rows(db_path)
    bytes_before = _stored_text_bytes(rows_before)
    assert rows_before == [(kept["id"], KEPT_TEXT, LATER_CREATED_AT, LATER_DEADLINE)]

    doomed = [_created_paste(client, fake_clock, text) for text in DOOMED_TEXTS]
    assert len(_stored_rows(db_path)) == len(doomed) + 1
    assert _stored_text_bytes(_stored_rows(db_path)) > bytes_before

    # The clock reaches the deadline of every paste created at CREATED_AT, and
    # stays an hour short of the kept paste's.
    fake_clock.instant = DEADLINE

    assert _wait_for(lambda: _stored_rows(db_path) == rows_before)

    assert _stored_rows(db_path) == rows_before
    assert _stored_text_bytes(_stored_rows(db_path)) == bytes_before
    stored_ids = _stored_ids(_stored_rows(db_path))
    assert stored_ids == {kept["id"]}
    for paste in doomed:
        assert paste["id"] not in stored_ids

    # The reclamation and the answers agree: the kept paste still reads back,
    # and a collected paste's link is the uniform 404 (PRD.md item 5).
    assert client.get(_paste_path(kept)).content == KEPT_TEXT.encode("utf-8")
    assert client.get(_paste_path(doomed[0])).content == NOT_FOUND_BODY


@FAST_SWEEP
def test_tc903_the_sweeper_leaves_pastes_inside_their_three_hours_alone(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """The sweep is not "delete what is old": the stored deadline decides.

    The tick is proved to have happened rather than assumed: the case waits
    for the sweeper to collect the paste whose deadline has passed, and then
    checks that the paste with time left is still stored and still served,
    over several further ticks (PRD.md item 4, ARCHITECTURE.md Sweep).
    """
    doomed = _created_paste(client, fake_clock, DOOMED_TEXT)
    live = _created_paste(client, fake_clock, LIVE_TEXT, instant=LATER_CREATED_AT)
    live_rows = [(live["id"], LIVE_TEXT, LATER_CREATED_AT, LATER_DEADLINE)]

    fake_clock.instant = DEADLINE

    # Only a running sweeper can remove this row: no read asks for it.
    assert _wait_for(lambda: doomed["id"] not in _stored_ids(_stored_rows(db_path)))

    for _ in range(TICKS_OBSERVED):
        time.sleep(FAST_SWEEP_INTERVAL_SECONDS * 2)
        assert _stored_rows(db_path) == live_rows

    assert _stored_text_bytes(_stored_rows(db_path)) == len(LIVE_TEXT.encode("utf-8"))
    response = client.get(_paste_path(live))
    assert response.status_code == 200
    assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert response.content == LIVE_TEXT.encode("utf-8")


@FAST_SWEEP
def test_tc904_the_sweeper_sweeps_on_the_injected_instant_and_not_the_real_clock(
    client: TestClient,
    db_path: Path,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD.md Success: the sweeper reads ``clock.now()``, so no test needs real time.

    The machine's clock is far past both deadlines, so a sweeper that read it
    would empty the table; ``clock.time`` is forbidden outright, so one that
    asked the clock module for the real time would raise instead of sweeping.
    The injected instant decides: exactly the paste whose deadline it has
    reached is collected, and the paste created an hour later keeps its row.
    """
    assert (
        time.time() > LATER_DEADLINE
    ), "the machine's clock must be past these deadlines"

    doomed = _created_paste(client, fake_clock, DOOMED_TEXT)
    live = _created_paste(client, fake_clock, LIVE_TEXT, instant=LATER_CREATED_AT)
    fake_clock.instant = DEADLINE

    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    assert _wait_for(lambda: doomed["id"] not in _stored_ids(_stored_rows(db_path)))

    assert _stored_rows(db_path) == [
        (live["id"], LIVE_TEXT, LATER_CREATED_AT, LATER_DEADLINE)
    ]


@FAST_SWEEP
def test_tc905_a_failed_sweep_is_reported_and_the_task_keeps_sweeping(
    client: TestClient,
    db_path: Path,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store that fails one sweep must not take reclamation down with it.

    The first sweep the task makes after this point raises, as a locked
    database would; the paste still leaves the table, which is only possible
    if the loop carried on after the failure. The failure is reported rather
    than swallowed, because there is no caller to be told
    (app/main.py, ``_sweep_expired_pastes``).
    """
    doomed = _created_paste(client, fake_clock, DOOMED_TEXT)

    real_delete_expired = Store.delete_expired
    attempted: list[float] = []

    def fail_the_first_sweep(self: Store, now: float) -> int:
        attempted.append(now)
        if len(attempted) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_delete_expired(self, now)

    monkeypatch.setattr(Store, "delete_expired", fail_the_first_sweep)
    caplog.set_level(logging.ERROR)

    fake_clock.instant = DEADLINE

    assert _wait_for(lambda: _stored_rows(db_path) == [])
    assert len(attempted) >= 2
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_tc906_a_read_reclaims_an_expired_paste_without_any_sweep(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 9's other half: the read deletes the expired row itself.

    The sweeper's period is the documented minute here, so no tick can have run
    inside this test and whatever removed the row was the read: the row is
    deleted before the uniform 404 is written, and the rows and the text bytes
    return to the level they had before the expired paste was created
    (ARCHITECTURE.md Read, PRD.md item 6).
    """
    assert config.SWEEP_INTERVAL_SECONDS == DOCUMENTED_SWEEP_INTERVAL_SECONDS

    kept = _created_paste(client, fake_clock, KEPT_TEXT, instant=LATER_CREATED_AT)
    rows_before = _stored_rows(db_path)
    bytes_before = _stored_text_bytes(rows_before)

    doomed = _created_paste(client, fake_clock, DOOMED_TEXT)
    assert len(_stored_rows(db_path)) == 2

    fake_clock.instant = DEADLINE
    response = client.get(_paste_path(doomed))

    assert response.status_code == 404
    assert response.json() == {"error": NOT_FOUND_CODE}
    assert response.content == NOT_FOUND_BODY
    assert DOOMED_TEXT not in response.text
    assert doomed["id"] not in response.text

    assert _stored_rows(db_path) == rows_before
    assert _stored_text_bytes(_stored_rows(db_path)) == bytes_before
    # The paste that is still inside its three hours is untouched by the miss.
    assert client.get(_paste_path(kept)).content == KEPT_TEXT.encode("utf-8")
