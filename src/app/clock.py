"""The time source: one place the process reads wall-clock time (ARCHITECTURE.md
Parts, table row "Clock", and its decision row "Injectable ``clock.now()``,
default ``time.time``").

Every deadline is computed and every expiry comparison made through
``now()``, so routes never call ``time.time()`` themselves (AGENTS.md
Conventions). The same function is exposed as the FastAPI dependency
``Now``, which is the name a test replaces through
``app.dependency_overrides[clock.now]``: the lifetime is fixed at three hours
and not settable (PRD.md item 4), so the only way a test observes an expiry is
by advancing the clock rather than by shortening the TTL or sleeping
(PRD.md Success, TASKS.md item 3).
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends


def now() -> float:
    """The current wall-clock time, in Unix seconds.

    PRD.md item 4 measures the paste lifetime in wall-clock time from
    creation, so this is ``time.time()``: seconds since the epoch as a float,
    the same unit the stored ``created_at`` and ``expires_at`` values use
    (ARCHITECTURE.md Data).
    """
    return time.time()


# What a route takes as a parameter to read the clock, e.g.
# ``def read_paste(paste_id: str, now: Now)``. FastAPI resolves it by calling
# the ``now`` function above at request time, which keeps that function the
# single override key: replacing ``clock.now`` in ``app.dependency_overrides``
# changes the instant every route sees, with no real clock read and no waiting
# (PRD.md Success).
Now = Annotated[float, Depends(now)]
