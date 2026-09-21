"""The paste store: one SQLite file, one table, one connection in WAL mode.

TASKS.md item 5 ships this module with the ``pastes`` table and the
``expires_at`` index created when the store is opened, the operations the
routes and the sweeper call — ``insert``, ``get``, ``consume``, ``delete``,
``delete_expired``, ``count`` and ``text_bytes`` — and one connection in WAL
mode for the whole process (ARCHITECTURE.md Parts, table row "Store", and its
decision row "Synchronous ``sqlite3``, one connection in WAL mode, access
serialised in the app").

The schema is ARCHITECTURE.md Data's, with one later addition: ``created_at``
and ``expires_at`` are Unix seconds as floats, ``expires_at`` is computed once
at creation and stored as an instant, and ``burn_after_read`` is the opt-in
flag for a paste whose first successful read consumes it. So reopening the
same file hands every paste back with the deadline it was given and the flag
it was created with, and neither a restart nor a clock change can move one
(PRD.md item 4, item 8). ``text`` is stored verbatim — no trimming,
normalisation or deduplication (PRD.md item 2, item 9) — and a row is deleted
rather than filtered once its deadline passes or, for a burn-after-read paste,
once its first successful read consumes it. Reclamation in PRD.md item 6 is
measured as rows and text bytes through ``count`` and ``text_bytes``.

The schema also migrates an existing database file from before the
burn-after-read column existed: after the table is created, the store adds the
column if it is missing, so upgrading the process does not discard or fail on
pastes the earlier version stored.

Where the parts in ARCHITECTURE.md fit:

- ``insert`` is what the create route calls with ``(id, text, now, now + 3h,
  burn_after_read)`` (ARCHITECTURE.md Create); ``consume`` is the read path's
  lookup — it returns the row and, for a burn-after-read paste, deletes it in
  the same lock so two racing readers cannot both receive the text; ``get``
  and ``delete`` are the read path's plain lookup and its removal of an
  expired row (ARCHITECTURE.md Read); ``delete_expired`` is the sweeper's one
  statement (ARCHITECTURE.md Sweep), called with ``clock.now()`` so the store
  never reads a clock of its own (AGENTS.md Conventions); ``count`` and
  ``text_bytes`` measure what is stored, for the reclamation check in
  TASKS.md item 9.
- Opening a store is startup work: the constructor creates the file if it is
  missing, switches it to WAL, creates the table and the index if they are
  missing, and migrates the burn-after-read column if it is missing. The
  lifespan opens the store once and closes it on shutdown (TASKS.md items 5
  and 9). Nothing here touches the clock, the configuration or the request:
  the path is handed in, so the test suite can point the store at a throwaway
  file and never reach the operator's database (PRD.md Success).
- The one connection is used from more than one thread once the app runs:
  every call into the store goes through FastAPI's threadpool — a synchronous
  route is served there, and the create route, which reads its request body
  asynchronously, hands its insert to it — while the sweeper loops in the
  event loop. The connection is therefore opened with
  ``check_same_thread=False`` and every method holds a lock for the length of
  its statement, which is the serialisation ARCHITECTURE.md's decision row
  names.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

# ARCHITECTURE.md Data, plus the burn-after-read flag: one table, no others,
# and the single index over the deadline the sweeper's range scan uses. Both
# statements are idempotent, so reopening an existing file is a no-op that
# leaves its rows untouched (PRD.md item 8). The flag is stored as an INTEGER
# with a default of 0, the SQLite spelling of a boolean column: a paste is
# burn-after-read only when its creator asked for it.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pastes (
  id               TEXT PRIMARY KEY,
  text             TEXT NOT NULL,
  created_at       REAL NOT NULL,
  expires_at       REAL NOT NULL,
  burn_after_read  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS pastes_expires_at ON pastes (expires_at);
"""

# The migration for a database file created before the burn-after-read column
# existed. ``ALTER TABLE ... ADD COLUMN`` keeps every existing row, and the
# NOT NULL default gives each of them the safe value: old pastes were never
# burn-after-read, so they remain ordinary pastes.
_ADD_BURN_AFTER_READ_COLUMN_SQL = """
ALTER TABLE pastes ADD COLUMN burn_after_read INTEGER NOT NULL DEFAULT 0
"""

# The deadline is stored, never recomputed: the values the create route passes
# go in as they are (ARCHITECTURE.md Decision "``expires_at`` stored as an
# absolute instant").
_INSERT_SQL = """
INSERT INTO pastes (id, text, created_at, expires_at, burn_after_read)
VALUES (?, ?, ?, ?, ?)
"""

_SELECT_SQL = """
SELECT id, text, created_at, expires_at, burn_after_read FROM pastes WHERE id = ?
"""

_DELETE_SQL = """
DELETE FROM pastes WHERE id = ?
"""

# The conditional delete behind ``consume``: only a row the creator marked
# burn-after-read is removed by a read, and the flag cannot change after
# creation, so the ``AND`` is a guard rather than a decision that could race.
_DELETE_BURN_AFTER_READ_SQL = """
DELETE FROM pastes WHERE id = ? AND burn_after_read = 1
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


def _ensure_burn_after_read_column(connection: sqlite3.Connection) -> None:
    """Add the burn-after-read column to an older table, if it is missing.

    A database file created by the version without the flag has a ``pastes``
    table with only the four original columns. ``CREATE TABLE IF NOT EXISTS``
    leaves that table alone, so this is the upgrade path: the column is added
    once, with ``0`` for every existing row. A newly created table already has
    the column, and this becomes a no-op.
    """
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(pastes)").fetchall()
    }
    if "burn_after_read" not in columns:
        connection.execute(_ADD_BURN_AFTER_READ_COLUMN_SQL)


@dataclass(frozen=True, slots=True)
class Paste:
    """One stored paste, as ``get`` and ``consume`` return it.

    ``expires_at`` is the deadline fixed at creation (PRD.md item 4), which the
    read path compares against ``clock.now()``; ``text`` is the stored text
    unchanged, which the read path serves as the response body (PRD.md item 2).
    ``created_at`` is carried along because both instants are part of the row
    (ARCHITECTURE.md Data). ``burn_after_read`` is the stored opt-in flag: when
    it is true, the first successful read deletes the row, so the paste can be
    served at most once.
    """

    id: str
    text: str
    created_at: float
    expires_at: float
    burn_after_read: bool = False


class Store:
    """The process's one connection to the paste database.

    Constructing a ``Store`` is opening the database: the file (and its
    directory's WAL sidecars) is created if missing, ``journal_mode`` is set to
    WAL, the table and index are created if they are missing, and an older
    table is migrated to carry the burn-after-read column. The lifespan does
    this once at startup and calls ``close()`` at shutdown (TASKS.md item 9),
    while tests open and close throwaway files (PRD.md Success).

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
            _ensure_burn_after_read_column(connection)
            connection.commit()
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
        self,
        paste_id: str,
        text: str,
        created_at: float,
        expires_at: float,
        burn_after_read: bool = False,
    ) -> None:
        """Store one paste under ``paste_id`` and commit it.

        The text is written verbatim and both instants are written as given, so
        the deadline stored here is the one every later read enforces (PRD.md
        items 2 and 4). ``burn_after_read`` is stored as the opt-in flag the
        read path later honours; the default is false, because a paste must
        never burn on the first read unless its creator asked for it. A second
        insert under an id that is already stored raises
        ``sqlite3.IntegrityError``: the primary key refuses it rather than
        replacing the paste that is there.
        """
        with self._lock:
            self._connection.execute(
                _INSERT_SQL,
                (paste_id, text, created_at, expires_at, burn_after_read),
            )
            self._connection.commit()

    def get(self, paste_id: str) -> Paste | None:
        """The paste stored under ``paste_id``, or ``None`` if there is none.

        No deadline is applied here: the read path compares the returned
        ``expires_at`` against ``clock.now()`` itself, so the store holds no
        clock and an expired row is deleted by the caller before it answers the
        uniform 404 (ARCHITECTURE.md Read). ``consume`` is the read path's
        lookup; ``get`` remains the plain, side-effect-free read for callers
        that must inspect a row without consuming it.
        """
        with self._lock:
            row = self._connection.execute(_SELECT_SQL, (paste_id,)).fetchone()

        if row is None:
            return None
        return Paste(
            id=row[0],
            text=row[1],
            created_at=row[2],
            expires_at=row[3],
            burn_after_read=bool(row[4]),
        )

    def consume(self, paste_id: str) -> Paste | None:
        """Return the paste under ``paste_id``, deleting burn-after-read rows.

        This is the read path's one lookup. For an ordinary paste the row is
        returned with no write, exactly like ``get``. For a paste whose creator
        opted into burn-after-read, the row is deleted in the same lock that
        read it, before the route can answer 200: the first successful reader
        is the only one that receives the text, and a racing second reader gets
        ``None`` and the same 404 as an id that was never issued. The deletion
        happens on the read path, not at creation or by the sweeper, so an
        unread burn-after-read paste still lives until its deadline and is
        still collected by ``delete_expired`` once the deadline passes.
        """
        with self._lock:
            row = self._connection.execute(_SELECT_SQL, (paste_id,)).fetchone()

            if row is None:
                return None

            paste = Paste(
                id=row[0],
                text=row[1],
                created_at=row[2],
                expires_at=row[3],
                burn_after_read=bool(row[4]),
            )

            if paste.burn_after_read:
                self._connection.execute(_DELETE_BURN_AFTER_READ_SQL, (paste_id,))
                self._connection.commit()

            return paste

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
