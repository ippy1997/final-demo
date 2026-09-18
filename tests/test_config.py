"""Task 2: the configuration constants and the database path (TASKS.md item 2).

TASKS.md item 2 ships ``app/config.py`` with the three-hour TTL, the 1 MiB
ceiling, the sweep interval and the SQLite path taken from ``PASTEBIN_DB``,
and asks for "a test asserting the TTL is a constant with no override".
PRD.md item 4 and its Defaults applied section rule out a per-paste lifetime
and an operator setting, so the constant must survive a re-import with the
environment variables a caller might reach for.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app import config

# The literal TASKS.md item 2 and ARCHITECTURE.md quote for the paste lifetime.
THREE_HOURS_IN_SECONDS = 3 * 60 * 60

# Names a caller might reasonably try in order to set a lifetime. None of them
# may change the constant (PRD.md "Not in the first version": per-paste or
# operator-chosen lifetimes).
TTL_OVERRIDE_ENV_VARS = (
    "PASTEBIN_TTL",
    "PASTEBIN_TTL_SECONDS",
    "PASTE_TTL_SECONDS",
)


def test_paste_ttl_is_three_hours_as_an_integer_number_of_seconds() -> None:
    assert config.PASTE_TTL_SECONDS == THREE_HOURS_IN_SECONDS
    assert isinstance(config.PASTE_TTL_SECONDS, int)


def test_paste_ttl_is_not_overridable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh run of the module body with lifetime names set keeps it fixed."""
    for name in TTL_OVERRIDE_ENV_VARS:
        monkeypatch.setenv(name, "1")

    reloaded = importlib.reload(config)

    assert reloaded.PASTE_TTL_SECONDS == THREE_HOURS_IN_SECONDS
    assert isinstance(reloaded.PASTE_TTL_SECONDS, int)


def test_max_paste_bytes_is_one_mib() -> None:
    assert config.MAX_PASTE_BYTES == 1024 * 1024
    assert isinstance(config.MAX_PASTE_BYTES, int)


def test_sweep_interval_is_the_documented_sixty_seconds() -> None:
    assert config.SWEEP_INTERVAL_SECONDS == 60
    assert config.SWEEP_INTERVAL_SECONDS < config.PASTE_TTL_SECONDS


def test_database_path_defaults_to_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No setup needed to start: the default file is relative to the cwd."""
    monkeypatch.delenv(config.DB_PATH_ENV_VAR, raising=False)

    path = config.database_path()

    assert path == Path("pastebin.db")
    assert not path.is_absolute()


def test_database_path_comes_from_pastebin_db_after_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Set after import, so tests can use a throwaway file (PRD.md Success)."""
    throwaway = tmp_path / "throwaway.db"
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(throwaway))

    assert config.database_path() == throwaway


def test_blank_pastebin_db_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, "   ")

    assert config.database_path() == Path("pastebin.db")
