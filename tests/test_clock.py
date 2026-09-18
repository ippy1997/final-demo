"""Task 3: ``app/clock.py``, the injectable time source (TASKS.md item 3).

The module ships ``now()`` returning ``time.time()`` and exposes it as the
FastAPI dependency tests override, so expiry is observed by moving the clock
rather than by waiting or by shortening the fixed three-hour lifetime
(PRD.md item 4 and Success: "the expiry tests do not depend on the machine's
real clock"; ARCHITECTURE.md Parts, table row "Clock").

The cases below pin the unit and the wall-clock meaning of ``now()``, the
dependency's default (the real clock, with no override installed), the
override itself through the same key the later route tests will use
(``app.dependency_overrides[clock.now]``), and — the point of the task — that
a route pinned to a fake clock never reads the real one. The last case moves a
route back and forth across a deadline with every request served in the same
test, which is only possible because the instant is injected.

The app under test here is a throwaway probe app rather than ``app.main``:
``app.main`` has no route that reads the clock yet (they arrive with TASKS.md
items 6, 7 and 9). The probe routes only exist to ask "what instant does a
route see?", which is what this task has to be able to answer; the probe app
never touches the real application's route table.
"""

from __future__ import annotations

import time
from typing import get_args

import pytest
from fastapi import FastAPI, params
from fastapi.testclient import TestClient

from app import clock, config

# An arbitrary instant to pretend it is, well before the real clock and far
# enough from the epoch to catch a value reported in milliseconds instead of
# seconds.
FIXED_INSTANT = 1_700_000_000.0

# The deadline the probe's /alive route compares against, built from one paste
# creation exactly as a stored paste's deadline is (PRD.md item 4: three hours
# from creation). The two instants the case uses are the boundary PRD.md names:
# T+3h-1s still resolves, T+3h does not.
CREATED_AT = FIXED_INSTANT
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS


class _RealClockRead(AssertionError):
    """Raised when the process asks for real time while a fake clock is set."""


class _ExplodingClock:
    """A stand-in for the ``time`` module that refuses to report an instant.

    Installed in place of ``clock.time`` to prove that a request served under
    an overridden clock does not reach ``time.time()`` at all (PRD.md Success:
    expiry is tested without the machine's real clock).
    """

    def time(self) -> float:
        raise _RealClockRead("the real clock was read while a fake clock was installed")


@pytest.fixture
def probe() -> FastAPI:
    """A minimal app whose routes answer with the instant they were given."""
    app = FastAPI()

    @app.get("/now")
    def read_now(now: clock.Now) -> dict[str, float]:
        return {"now": now}

    @app.get("/alive")
    def still_alive(now: clock.Now) -> dict[str, bool]:
        # PRD.md boundary default: retrievable strictly before the deadline.
        return {"alive": now < DEADLINE}

    return app


def test_now_returns_wall_clock_unix_seconds_as_a_float() -> None:
    """The unit stored deadlines use: seconds since the epoch (ARCHITECTURE.md Data)."""
    before = time.time()
    instant = clock.now()
    after = time.time()

    assert isinstance(instant, float)
    assert before <= instant <= after


def test_now_is_exposed_as_the_dependency_routes_take() -> None:
    """``clock.Now`` resolves through ``clock.now``, the override key of record."""
    value_type, dependency = get_args(clock.Now)

    assert value_type is float
    assert isinstance(dependency, params.Depends)
    assert dependency.dependency is clock.now


def test_a_route_taking_the_dependency_sees_the_real_clock_by_default(
    probe: FastAPI,
) -> None:
    """Nothing is overridden, so the default ``time.time()`` applies."""
    before = time.time()
    with TestClient(probe) as client:
        payload = client.get("/now").json()
    after = time.time()

    assert probe.dependency_overrides == {}
    assert before <= payload["now"] <= after


def test_an_override_replaces_the_instant_the_route_sees(probe: FastAPI) -> None:
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT

    with TestClient(probe) as client:
        payload = client.get("/now").json()

    assert payload == {"now": FIXED_INSTANT}


def test_the_overridden_instant_does_not_advance_between_reads(probe: FastAPI) -> None:
    """Reads repeat the same instant: no waiting, no drift between requests."""
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT

    with TestClient(probe) as client:
        first = client.get("/now").json()
        second = client.get("/now").json()

    assert first == second == {"now": FIXED_INSTANT}


def test_a_route_on_an_overridden_clock_never_reads_the_real_one(
    monkeypatch: pytest.MonkeyPatch, probe: FastAPI
) -> None:
    """A request under a fake clock does not reach ``time.time()`` (PRD.md Success)."""
    monkeypatch.setattr(clock, "time", _ExplodingClock())
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT

    with TestClient(probe) as client:
        payload = client.get("/now").json()

    assert payload == {"now": FIXED_INSTANT}


def test_the_exploding_clock_would_catch_a_real_read(
    monkeypatch: pytest.MonkeyPatch, probe: FastAPI
) -> None:
    """The guard above bites: with no override, the same request fails on the read."""
    monkeypatch.setattr(clock, "time", _ExplodingClock())

    with TestClient(probe) as client, pytest.raises(_RealClockRead):
        client.get("/now")


def test_clearing_the_override_puts_the_real_clock_back(probe: FastAPI) -> None:
    """Overrides are per app and removable, so a fake clock cannot leak onward."""
    probe.dependency_overrides[clock.now] = lambda: FIXED_INSTANT

    with TestClient(probe) as client:
        assert client.get("/now").json() == {"now": FIXED_INSTANT}

        probe.dependency_overrides.clear()
        payload = client.get("/now").json()

    assert payload["now"] != FIXED_INSTANT
    assert abs(payload["now"] - time.time()) < 60


def test_moving_only_the_clock_moves_a_request_across_its_deadline(
    probe: FastAPI,
) -> None:
    """T+3h-1s resolves and T+3h does not, with both served in one test (item 4)."""

    def set_instant(instant: float) -> None:
        probe.dependency_overrides[clock.now] = lambda: instant

    with TestClient(probe) as client:
        set_instant(DEADLINE - 1)
        assert client.get("/alive").json() == {"alive": True}

        set_instant(DEADLINE)
        assert client.get("/alive").json() == {"alive": False}

        # Backwards too: the instant is injected on every request, not consumed.
        set_instant(DEADLINE - 1)
        assert client.get("/alive").json() == {"alive": True}
