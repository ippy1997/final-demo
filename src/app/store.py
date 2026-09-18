"""The paste store: one SQLite file, one table, one connection in WAL mode.

TASKS.md item 5 ships this module with the ``pastes`` table and the
``expires_at`` index created when the store is opened, the operations the
routes and the sweeper call — ``insert``, ``get``, ``delete``,
``delete_expired``, ``count`` and ``text_bytes`` — and one connection in WAL
mode for the whole process (ARCHITECTURE.md Parts, table row "Store", and its
decision row "Synchronous ``sqlite3``, one connection in WAL mode, access
serialised in the app").

The schema is ARCHITECTURE.md Data's, character for character: ``created_at``
and ``expires_at`` are Unix seconds as floats and ``expires_at`` is computed
once at creation and stored as an instant, so reopening the same file hands
every paste back with the deadline it was given and neither a restart nor a
clock change can move one (PRD.md item 4, item 8). ``text`` is stored
verbatim — no trimming, normalisation or deduplication (PRD.md item 2, item 9)
— and the row is deleted rather than filtered once its deadline passes, which
is what makes the reclamation in PRD.md item 6 measurable as rows and text
bytes through ``count`` and ``text_bytes``.

Where the parts in ARCHITECTURE.md fit:

- ``insert`` is what the create route calls with ``(id, text, now, now + 3h)``
  (ARCHITECTURE.md Create); ``get`` and ``delete`` are the read path's lookup
  and its removal of an expired row (ARCHITECTURE.md Read); ``delete_expired``
  is the sweeper's one statement (ARCHITECTURE.md Sweep), called with
  ``clock.now()`` so the store never reads a clock of its own (AGENTS.md
  Conventions); ``count`` and ``text_bytes`` measure what is stored, for the
  reclamation check in TASKS.md item 9.
- Opening a store is startup work: the constructor creates the file if it is
  missing, switches it to WAL and creates the table and the index if they are
  missing, which is why the lifespan opens the store once and closes it on
  shutdown (TASKS.md items 5 and 9). Nothing here touches the clock, the
  configuration or the request: the path is handed in, so the test suite can
  point the store at a throwaway file and never reach the operator's database
  (PRD.md Success).
- The one connection is used from more than one thread once the app runs:
  every route is a synchronous function, so FastAPI serves them from its
  threadpool, while the sweeper loops in the event loop. The connection is
  therefore opened with ``check_same_thread=False`` and every method holds a
  lock for the length of its statement, which is the serialisation
  ARCHITECTURE.md's decision row names.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

# ARCHITECTURE.md Data: one table, no others, and the single index over the
# deadline the sweeper's range scan uses. Both statements are idempotent, so
# reopening an existing file is a no-op that leaves its rows untouched
# (PRD.md item 8).
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pastes (
  id         TEXT PRIMARY KEY,
  text       TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pastes_expires_at ON pastes (expires_at);
"""

# The deadline is stored, never recomputed: the value the create route passes
# goes in as it is (ARCHITECTURE.md Decision "``expires_at`` stored as an
# absolute instant").
_INSERT_SQL = """
INSERT INTO pastes (id, text, created_at, expires_at) VALUES (?, ?, ?, ?)
"""

_SELECT_SQL = """
SELECT id, text, created_at, expires_at FROM pastes WHERE id = ?
"""

_DELETE_SQL = """
DELETE FROM pastes WHERE id = ?
"""

# `<=`, not `<`: PRD.md's boundary default makes a paste unretrievable from its
# deadline on, so a row whose deadline is exactly `now` has expired.
_DELETE_EXPIRED_SQL = """
DELETE FROM pastes WHERE expires_at <= ?
"""

_COUNT_SQL = """
SELECT COUNT(*) FROM pastes
"""

# The size of the stored text in bytes, not characters: casting the TEXT value
# to a BLOB gives its UTF-8 bytes, which is the amount PRD.md item 6 measures
# returning to its earlier level. An empty table sums to NULL, hence the
# COALESCE.
_TEXT_BYTES_SQL = """
SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM pastes
"""


@dataclass(frozen=True, slots=True)
class Paste:
    """One stored paste, as ``get`` returns it.

    ``expires_at`` is the deadline fixed at creation (PRD.md item 4), which the
    read path compares against ``clock.now()``; ``text`` is the stored text
    unchanged, which the read path serves as the response body (PRD.md item 2).
    ``created_at`` is carried along because both instants are part of the row
    (ARCHITECTURE.md Data).
    """

    id: str
    text: str
    created_at: float
    expires_at: float


class Store:
    """The process's one connection to the paste database.

    Constructing a ``Store`` is opening the database: the file (and its
    directory's WAL sidecars) is created if missing, ``journal_mode`` is set to
    WAL, and the table and index are created if they are missing. The lifespan
    does this once at startup and calls ``close()`` at shutdown (TASKS.md item
    9), while tests open and close throwaway files (PRD.md Success).

    Every method is safe to call from any thread, and all of them use the one
    connection: the lock serialises them, which is what makes a single
    connection usable from FastAPI's threadpool and the sweeper loop at the
    same time. Methods called after ``close()`` fail with the
    ``sqlite3.ProgrammingError`` the closed connection raises.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

        connection = sqlite3.connect(self._path, check_same_thread=False)
        try:
            # WAL is what lets the sweeper write while a reader is mid-request,
            # and it is a property of the file: a later connection, including
            # one made by a restarted process, reports "wal" again without
            # being told (ARCHITECTURE.md Stack: "one connection in WAL mode").
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA_SQL)
        except BaseException:
            # A file that could not be prepared is not left open behind a
            # failed startup.
            connection.close()
            raise

        self._connection = connection

    @property
    def path(self) -> Path:
        """The file this store opened, as it was given."""
        return self._path

    def close(self) -> None:
        """Close the one connection, check-pointing the WAL back into the file.

        Called once at shutdown, and by tests between two openings of the same
        file (PRD.md item 8: a restart reopens the file, so closing cleanly is
        the shape the restart tests take).
        """
        with self._lock:
            self._connection.close()

    def insert(
        self, paste_id: str, text: str, created_at: float, expires_at: float
    ) -> None:
        """Store one paste under ``paste_id`` and commit it.

        The text is written verbatim and both instants are written as given, so
        the deadline stored here is the one every later read enforces (PRD.md
        items 2 and 4). A second insert under an id that is already stored
        raises ``sqlite3.IntegrityError``: the primary key refuses it rather
        than replacing the paste that is there.
        """
        with self._lock:
            self._connection.execute(
                _INSERT_SQL, (paste_id, text, created_at, expires_at)
            )
            self._connection.commit()

    def get(self, paste_id: str) -> Paste | None:
        """The paste stored under ``paste_id``, or ``None`` if there is none.

        No deadline is applied here: the read path compares the returned
        ``expires_at`` against ``clock.now()`` itself, so the store holds no
        clock and an expired row is deleted by the caller before it answers the
        uniform 404 (ARCHITECTURE.md Read).
        """
        with self._lock:
            row = self._connection.execute(_SELECT_SQL, (paste_id,)).fetchone()

        if row is None:
            return None
        return Paste(
            id=row[0], text=row[1], created_at=row[2], expires_at=row[3]
        )

    def delete(self, paste_id: str) -> int:
        """Delete the paste stored under ``paste_id``; return how many rows went.

        The read path calls this for an expired paste before answering 404 so
        the text is gone rather than hidden (ARCHITECTURE.md Read, PRD.md item
        6). An unknown id deletes nothing and returns ``0``.
        """
        with self._lock:
            cursor = self._connection.execute(_DELETE_SQL, (paste_id,))
            self._connection.commit()
            return cursor.rowcount

    def delete_expired(self, now: float) -> int:
        """Delete every paste whose deadline is ``now`` or earlier; return the count.

        The sweeper's one statement, called with ``clock.now()`` every
        ``config.SWEEP_INTERVAL_SECONDS`` (ARCHITECTURE.md Sweep). The
        comparison is inclusive because a paste is unretrievable from its
        deadline on (PRD.md item 4's boundary default), and the index over
        ``expires_at`` is what keeps this a range scan rather than a full read
        of every paste ever written.
        """
        with self._lock:
            cursor = self._connection.execute(_DELETE_EXPIRED_SQL, (now,))
            self._connection.commit()
            return cursor.rowcount

    def count(self) -> int:
        """How many pastes are stored, expired rows included if not yet swept.

        Half of the reclamation measurement in PRD.md item 6: after the
        deadlines pass and the rows are deleted, this returns to its earlier
        value (TASKS.md item 9).
        """
        with self._lock:
            row = self._connection.execute(_COUNT_SQL).fetchone()
        return int(row[0])

    def text_bytes(self) -> int:
        """The total size of the stored text, in bytes.

        The other half of the reclamation measurement: the UTF-8 byte length of
        every stored ``text``, summed, which returns to its earlier value once
        expired rows are deleted (PRD.md item 6). Bytes rather than characters,
        because what the operator's disk holds is what has to stop growing.
        """
        with self._lock:
            row = self._connection.execute(_TEXT_BYTES_SQL).fetchone()
        return int(row[0])
