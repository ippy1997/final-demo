"""Task 3 acceptance cases for ``app/clock.py`` (TASKS.md item 3).

``app/clock.py`` ships ``now()`` returning ``time.time()`` and exposes the same
function as the FastAPI dependency ``clock.Now``; the task's success criterion
is "injectable time, no real clock in expiry tests" (TASKS.md item 3, PRD.md
Success). These cases pin the delegation to the stdlib clock, the unit stored
deadlines are kept in (ARCHITECTURE.md Data), the shape of ``clock.Now`` and
the single override key ``clock.now`` that governs a route however the
dependency is written — ``clock.Now`` or an explicit ``Depends(clock.now)`` —
plus the behaviour the later expiry tests are built on: one fake clock
installed once can be advanced between requests, with no real clock read.

They complement ``tests/test_clock.py`` (same task), which already covers a
plain ``now()`` call, the resolved dependency, the default path with no
override, clearing an override, and the T+3h-1s / T+3h boundary.

The routes here live on throwaway probe apps, because ``app.main`` has no
route that reads the clock until TASKS.md items 6, 7 and 9: nothing in this
task can observe a real paste deadline through the service, which is recorded
as the untestable case in the run's case list.
"""

from __future__ import annotations

import inspect
import time
from typing import get_args

import pytest
from fastapi import Depends, FastAPI, params
from fastapi.testclient import TestClient

from app import clock

# An arbitrary instant to pretend it is: far from the epoch, and far enough
# away that a value reported in milliseconds instead of seconds stands out.
FIXED_INSTANT = 1_700_000_000.0

# The paste lifetime TASKS.md item 2, PRD.md item 4 and ARCHITECTURE.md Data
# all quote, and the boundary PRD.md's defaults apply: retrievable strictly
# before the deadline and gone from the deadline itself on.
THREE_HOURS_IN_SECONDS = 3 * 60 * 60
CREATED_AT = FIXED_INSTANT
DEADLINE = CREATED_AT + THREE_HOURS_IN_SECONDS


class ClockReadForbidden(AssertionError):
    """Raised when the real clock is read while a fake one is installed."""


class _StubClock:
    """Stands in for the ``time`` module with one fixed, observable answer."""

    def __init__(self, instant: float) -> None:
        self.instant = instant

    def time(self) -> float:
        return self.instant


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant.

    Installed in place of ``clock.time`` to prove that a request under an
    overridden clock never reaches ``time.time()``, which is what lets the
    expiry tests ignore the machine's real clock (PRD.md Success).
    """

    def time(self) -> float:
        raise ClockReadForbidden("the real clock was read while a fake one was set")


class _MutableClock:
    """A fake clock whose instant the test can move, as a later expiry test will.

    FastAPI calls the override on every request, so advancing this holder
    between two requests moves the clock with no reinstall and no waiting.
    """

    def __init__(self, instant: float) -> None:
        self.instant = instant

    def now(self) -> float:
        return self.instant


def unrelated_clock_lookalike() -> float:
    """A dependency that is not the clock; overriding it must change nothing."""
    return 0.0


def _probe_app() -> FastAPI:
    """An app whose routes answer with the instant they were handed."""
    app = FastAPI()

    @app.get("/now")
    def read_now(now: clock.Now) -> dict[str, float]:
        return {"now": now}

    @app.get("/now-explicit")
    def read_now_explicitly(now: float = Depends(clock.now)) -> dict[str, float]:
        # The same dependency written out instead of through the alias: still
        # resolved by calling `clock.now`, so the same override key governs it.
        return {"now": now}

    @app.get("/pastes/{paste_id}")
    def read_paste(paste_id: str, now: clock.Now) -> dict[str, object]:
        # The parameter list TASKS.md item 7's read route has: a path id and
        # the injected clock.
        return {"paste_id": paste_id, "now": now}

    @app.get("/retrievable")
    def still_retrievable(now: clock.Now) -> dict[str, bool]:
        # PRD.md's boundary default: retrievable strictly before the deadline.
        return {"retrievable": now < DEADLINE}

    return app


@pytest.fixture
def probe() -> FastAPI:
    return _probe_app()


def test_tc001_now_reports_exactly_what_time_time_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 3: ``now()`` returns ``time.time()``, value and all."""
    monkeypatch.setattr(clock, "time", _StubClock(FIXED_INSTANT))

    instant = clock.now()

    assert instant == FIXED_INSTANT
    assert isinstance(instant, float)


def test_tc002_now_is_in_unix_seconds_not_milliseconds() -> None:
    """Deadlines are stored as Unix seconds (ARCHITECTURE.md Data)."""
    before = time.time()
    instant = clock.now()
    after = time.time()

    assert isinstance(instant, float)
    # Any instant between 2001 and 2286, as seconds: a millisecond reading is
    # ~1000x larger and a monotonic tick count is orders of magnitude smaller.
    assert 1_000_000_000 < instant < 10_000_000_000
    assert before <= instant <= after


def test_tc003_clock_now_alias_resolves_through_the_override_key() -> None:
    """``clock.Now`` is ``float`` resolved by ``clock.now`` itself (item 3)."""
    value_type, dependency = get_args(clock.Now)

    assert value_type is float
    assert isinstance(dependency, params.Depends)
    assert dependency.dependency is clock.now


def test_tc004_a_route_taking_the_alias_sees_the_overridden_instant(
    probe: FastAPI,
) -> None:
    """The point of the task: a test can decide what instant a route sees."""
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT

    with TestClient(probe) as client:
        response = client.get("/now")

    assert response.status_code == 200
    assert response.json() == {"now": FIXED_INSTANT}


def test_tc005_an_explicit_depends_on_clock_now_uses_the_same_override(
    probe: FastAPI,
) -> None:
    """One override key covers both spellings of the dependency."""
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT

    with TestClient(probe) as client:
        aliased = client.get("/now").json()
        explicit = client.get("/now-explicit").json()

    assert aliased == explicit == {"now": FIXED_INSTANT}


def test_tc006_overriding_an_unrelated_dependency_leaves_the_real_clock(
    probe: FastAPI,
) -> None:
    """A stray override cannot freeze the clock: the key must be ``clock.now``."""
    probe.dependency_overrides[unrelated_clock_lookalike] = lambda: 0.0
    before = time.time()

    with TestClient(probe) as client:
        payload = client.get("/now").json()

    after = time.time()

    assert payload["now"] != 0.0
    assert before <= payload["now"] <= after


def test_tc007_one_fake_clock_advances_between_requests_in_a_single_test(
    probe: FastAPI,
) -> None:
    """The pattern the expiry tests use: move the clock, never wait (item 4)."""
    fake = _MutableClock(FIXED_INSTANT)
    probe.dependency_overrides[clock.now] = fake.now

    with TestClient(probe) as client:
        assert client.get("/now").json() == {"now": FIXED_INSTANT}

        fake.instant = DEADLINE - 1
        assert client.get("/now").json() == {"now": DEADLINE - 1}

        fake.instant = DEADLINE
        assert client.get("/now").json() == {"now": DEADLINE}

        fake.instant = DEADLINE + 100 * THREE_HOURS_IN_SECONDS
        assert client.get("/now").json() == {
            "now": DEADLINE + 100 * THREE_HOURS_IN_SECONDS
        }


def test_tc008_injected_time_decides_the_deadline_boundary(
    probe: FastAPI,
) -> None:
    """Retrievable at T+3h-1s, gone at T+3h, and the clock can move back."""
    fake = _MutableClock(CREATED_AT)
    probe.dependency_overrides[clock.now] = fake.now

    with TestClient(probe) as client:
        assert client.get("/retrievable").json() == {"retrievable": True}

        # One second before the deadline: still inside the three hours.
        fake.instant = DEADLINE - 1
        assert client.get("/retrievable").json() == {"retrievable": True}

        # The deadline itself: PRD.md's strict boundary.
        fake.instant = DEADLINE
        assert client.get("/retrievable").json() == {"retrievable": False}

        # Downtime-sized jump past the deadline stays expired: deadlines are
        # absolutes, not accumulated intervals.
        fake.instant = DEADLINE + 10 * THREE_HOURS_IN_SECONDS
        assert client.get("/retrievable").json() == {"retrievable": False}

        # And the instant is re-read per request, not consumed.
        fake.instant = CREATED_AT
        assert client.get("/retrievable").json() == {"retrievable": True}


def test_tc009_now_is_callable_with_no_arguments_outside_a_request() -> None:
    """Direct callers such as the item-9 sweeper call ``clock.now()``."""
    assert list(inspect.signature(clock.now).parameters) == []

    before = time.time()
    instant = clock.now()
    after = time.time()

    assert isinstance(instant, float)
    assert before <= instant <= after


def test_tc010_an_overridden_request_never_reads_the_real_clock(
    monkeypatch: pytest.MonkeyPatch, probe: FastAPI
) -> None:
    """No real clock in expiry tests (PRD.md Success)."""
    monkeypatch.setattr(clock, "time", _ForbiddenClock())
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT

    with TestClient(probe) as client:
        response = client.get("/now")

    assert response.status_code == 200
    assert response.json() == {"now": FIXED_INSTANT}


def test_tc011_the_forbidden_clock_bites_when_no_override_is_installed(
    monkeypatch: pytest.MonkeyPatch, probe: FastAPI
) -> None:
    """Keeps the case above honest: without an override the read does happen."""
    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    with TestClient(probe) as client, pytest.raises(ClockReadForbidden):
        client.get("/now")


def test_tc012_an_override_on_one_app_does_not_change_another_app(
    probe: FastAPI,
) -> None:
    """Overrides are per app, so a fake clock cannot leak into other tests."""
    other = _probe_app()
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT
    before = time.time()

    with TestClient(probe) as client:
        assert client.get("/now").json() == {"now": FIXED_INSTANT}

    with TestClient(other) as client:
        assert other.dependency_overrides == {}
        payload = client.get("/now").json()

    after = time.time()

    assert payload["now"] != FIXED_INSTANT
    assert before <= payload["now"] <= after


def test_tc013_a_path_parameter_and_the_clock_resolve_together(
    probe: FastAPI,
) -> None:
    """The read route's parameter list: the id plus the injected instant."""
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT

    with TestClient(probe) as client:
        response = client.get("/pastes/abc")

    assert response.status_code == 200
    assert response.json() == {"paste_id": "abc", "now": FIXED_INSTANT}
