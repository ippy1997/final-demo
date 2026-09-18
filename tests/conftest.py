"""Fixtures shared by the suite: a throwaway database, a fake clock and a test
client (AGENTS.md Layout).

The suite drives the real ``app.main:app`` through FastAPI's test client, so it
exercises the same lifespan the documented start command runs — a test client
entered with ``with``. Two things have to be true for that to be safe:

- **A throwaway database.** ``PASTEBIN_DB`` is pointed at a file under
  ``tmp_path`` before the app is started, on every test, so no test can reach
  the operator's ``pastebin.db`` even by forgetting to ask for a database
  (AGENTS.md Conventions, PRD.md Success: "the test suite ... does not touch
  the operator's live database"). The lifespan reads the variable when it
  opens the store (``app.main.lifespan``), which is why setting it around the
  test is enough.
- **A clock the test moves.** ``clock.now`` is the one override key
  (ARCHITECTURE.md Parts, table row "Clock"), so a fake clock installed here
  decides the instant every request sees: no test waits three hours and none
  reads the machine's real clock (PRD.md Success).

``FakeClock`` is mutable rather than fixed so one test can create a paste,
move the instant past its deadline and read it again — the pattern the expiry
tests are built on (TASKS.md items 8, 9 and 12).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app import clock, config
from app.main import app

# The instant a test starts at unless it moves the clock: far from the epoch
# and far enough from it that a value reported in the wrong unit stands out.
FIXED_INSTANT = 1_700_000_000.0

# The throwaway file's name inside each test's ``tmp_path``.
THROWAWAY_DB_FILENAME = "throwaway-pastes.db"


class FakeClock:
    """The time source a test installs in place of ``clock.now``.

    ``now()`` is what FastAPI calls on every request, so assigning
    ``instant`` between two requests moves the clock with no reinstall and no
    sleeping (PRD.md item 4, PRD.md Success).
    """

    def __init__(self, instant: float = FIXED_INSTANT) -> None:
        self.instant = instant

    def now(self) -> float:
        return self.instant


@pytest.fixture(autouse=True)
def db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A throwaway database file, which every test's app is pointed at.

    Autouse so the environment variable is set before anything starts the app:
    the lifespan opens ``config.database_path()``, and that must never be the
    working-directory default while the suite runs (AGENTS.md Conventions).
    Tests that inspect what was stored reopen this same file.
    """
    path = tmp_path / THROWAWAY_DB_FILENAME
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(path))
    return path


@pytest.fixture
def fake_clock() -> FakeClock:
    """A clock the test can move, installed on the app by ``client``."""
    return FakeClock()


@pytest.fixture
def client(fake_clock: FakeClock, db_path: Path) -> Iterator[TestClient]:
    """The real app, on a throwaway database, served with the fake clock.

    Entered as a context manager so the lifespan runs: the store is opened at
    startup and closed at shutdown exactly as the run path does it (TASKS.md
    item 9). The clock override is cleared afterwards so a fake instant cannot
    leak into the next test (ARCHITECTURE.md's decision row on the injectable
    clock).
    """
    app.dependency_overrides[clock.now] = fake_clock.now
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
