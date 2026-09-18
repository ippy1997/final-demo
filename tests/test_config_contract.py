"""Task 2 acceptance cases for ``app/config.py`` (TASKS.md item 2).

The module ships the fixed three-hour lifetime (PRD.md item 4), the 1 MiB
ceiling (PRD.md item 7), the sweeper period (ARCHITECTURE.md Sweep) and the
SQLite path taken from ``PASTEBIN_DB`` with a working-directory default
(ARCHITECTURE.md Data, README.md). These cases pin the exact values, the
boundary of each limit as this task can express it, the environment-override
refusal for the lifetime, and the path rules that make the documented
one-command start and a throwaway test database possible.

They complement ``tests/test_config.py`` (same task), which already asserts
the raw values, a re-import with lifetime names set to ``1``, and the blank
variable fallback. Cases the create route, the clock and the store would be
needed for are recorded as untestable on this task; see the run's case list.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app import config

# TASKS.md item 2 and ARCHITECTURE.md quote the lifetime as `3 * 60 * 60`.
THREE_HOURS_IN_SECONDS = 3 * 60 * 60
# TASKS.md item 2 and ARCHITECTURE.md Parts quote the ceiling as `1024 * 1024`.
ONE_MIB_IN_BYTES = 1024 * 1024
# ARCHITECTURE.md Sweep: the lifespan task sweeps "every 60 s".
DOCUMENTED_SWEEP_INTERVAL_SECONDS = 60
# ARCHITECTURE.md Data and README.md name the default file and the variable.
DOCUMENTED_DB_FILENAME = "pastebin.db"
DOCUMENTED_DB_ENV_VAR = "PASTEBIN_DB"


def test_tc001_paste_ttl_is_exactly_three_hours() -> None:
    """The fixed lifetime is 10800 s, the value every expiry boundary uses."""
    assert config.PASTE_TTL_SECONDS == THREE_HOURS_IN_SECONDS
    assert config.PASTE_TTL_SECONDS == 10800
    assert isinstance(config.PASTE_TTL_SECONDS, int)


def test_tc002_setting_a_lifetime_variable_to_zero_does_not_change_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero is the strongest attempt at a lifetime setting; the literal wins."""
    for name in ("PASTEBIN_TTL", "PASTEBIN_TTL_SECONDS", "PASTE_TTL_SECONDS"):
        monkeypatch.setenv(name, "0")

    reloaded = importlib.reload(config)

    assert reloaded.PASTE_TTL_SECONDS == THREE_HOURS_IN_SECONDS


def test_tc003_max_paste_bytes_is_exactly_one_mebibyte() -> None:
    """The ceiling is 1048576 bytes, not a decimal megabyte or one byte less."""
    assert config.MAX_PASTE_BYTES == ONE_MIB_IN_BYTES
    assert config.MAX_PASTE_BYTES == 1048576
    assert config.MAX_PASTE_BYTES != 1000 * 1000
    assert isinstance(config.MAX_PASTE_BYTES, int)


def test_tc004_sweep_interval_is_a_positive_integer_shorter_than_the_lifetime() -> None:
    assert config.SWEEP_INTERVAL_SECONDS == DOCUMENTED_SWEEP_INTERVAL_SECONDS
    assert isinstance(config.SWEEP_INTERVAL_SECONDS, int)
    assert 0 < config.SWEEP_INTERVAL_SECONDS < config.PASTE_TTL_SECONDS


def test_tc005_database_path_variable_is_pastebin_db() -> None:
    """The documented variable name is what the app reads (ARCHITECTURE.md Data)."""
    assert config.DB_PATH_ENV_VAR == DOCUMENTED_DB_ENV_VAR


def test_tc006_unset_variable_gives_pastebin_db_in_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No setup to start: the default file sits in whatever directory you run in."""
    monkeypatch.delenv(config.DB_PATH_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    path = config.database_path()

    assert path == Path(DOCUMENTED_DB_FILENAME)
    assert path.resolve() == (tmp_path / DOCUMENTED_DB_FILENAME).resolve()


def test_tc007_empty_variable_falls_back_to_the_working_directory_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty PASTEBIN_DB carries no path, so the default applies."""
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, "")
    monkeypatch.chdir(tmp_path)

    assert config.database_path() == Path(DOCUMENTED_DB_FILENAME)


def test_tc008_relative_variable_resolves_against_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, "throwaway-pastes.db")
    monkeypatch.chdir(tmp_path)

    assert config.database_path().resolve() == (
        tmp_path / "throwaway-pastes.db"
    ).resolve()


def test_tc009_path_follows_the_variable_on_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Set after import, so the suite can pick a throwaway file (PRD.md Success)."""
    first = tmp_path / "one.db"
    second = tmp_path / "two.db"

    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(first))
    assert config.database_path() == first

    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(second))
    assert config.database_path() == second


def test_tc010_absolute_variable_is_used_verbatim_and_ignores_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    absolute = tmp_path / "operator" / "abs.db"
    assert not absolute.exists()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(absolute))
    monkeypatch.chdir(elsewhere)

    assert config.database_path() == absolute


def test_tc011_importing_config_creates_no_database_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Config resolves a path only; the store owns creating the file (item 5)."""
    monkeypatch.delenv(config.DB_PATH_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    importlib.reload(config)

    assert not (tmp_path / DOCUMENTED_DB_FILENAME).exists()
    assert list(tmp_path.iterdir()) == []
