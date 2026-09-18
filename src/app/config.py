"""Configuration: the fixed paste lifetime, the size ceiling, the sweeper
interval and the SQLite database path (ARCHITECTURE.md Parts, table row
"Config").

The lifetime is a constant here, not a setting: PRD.md's "Not in the first
version" rules out per-paste and operator-chosen lifetimes, so there is no
create parameter and no environment variable that changes it (TASKS.md
item 2).
"""

from __future__ import annotations

import os
from pathlib import Path

# Lifetime of every paste, in seconds. PRD.md item 4: three hours measured
# from creation, fixed for every paste and unsettable, so this is a literal
# constant with no override.
PASTE_TTL_SECONDS = 3 * 60 * 60

# Largest accepted create body, in bytes. PRD.md item 7: a body over this is
# rejected with 413 before anything is stored.
MAX_PASTE_BYTES = 1024 * 1024

# How often the lifespan sweeper deletes rows whose deadline has passed
# (ARCHITECTURE.md Sweep).
SWEEP_INTERVAL_SECONDS = 60

# Environment variable naming the SQLite file, and the default used when it is
# unset: `pastebin.db` in the working directory, so the documented start
# command runs with no setup (PRD.md Users: service operator).
DB_PATH_ENV_VAR = "PASTEBIN_DB"
DEFAULT_DB_FILENAME = "pastebin.db"


def database_path() -> Path:
    """The SQLite file the app opens.

    ``PASTEBIN_DB`` is read on every call rather than at import, so the test
    suite can point the app at a throwaway file after the module is imported
    and never reaches the operator's database (PRD.md Success). A relative
    value, like the default, is resolved against the working directory. An
    unset, empty or blank value means the default.
    """
    configured = os.environ.get(DB_PATH_ENV_VAR, "").strip()
    if configured:
        return Path(configured)
    return Path(DEFAULT_DB_FILENAME)
