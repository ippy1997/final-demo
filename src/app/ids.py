"""Paste ids: 128 random bits rendered as unpadded URL-safe base64
(ARCHITECTURE.md Parts, table row "Ids", and its decision row "Ids from
``secrets.token_urlsafe(16)``").

``new_id()`` is the only place a paste id comes from, so ids are unguessable
and pastes are not enumerable (PRD.md item 3), and never a counter, a
timestamp or a hash of the text (AGENTS.md Conventions). The bytes come from
the stdlib ``secrets`` module, the one generator used here: the link is the
only credential (ARCHITECTURE.md Sign-in), so ids have to come from a source a
caller can neither predict nor walk. Nothing in this module depends on the
clock, the store or the request, which is what makes the 1000-ids-in-a-row
check in TASKS.md item 4 meaningful.
"""

from __future__ import annotations

import secrets


def new_id() -> str:
    """A fresh paste id: 16 random bytes (128 bits) as 22 URL-safe characters.

    ``secrets.token_urlsafe`` renders 16 bytes with the trailing base64 padding
    dropped — 22 characters, ``ceil(128 / 6)`` — over the alphabet ``A-Za-z0-9-_``,
    so the id goes into a link as-is with no escaping (PRD.md item 3).
    """
    return secrets.token_urlsafe(16)
