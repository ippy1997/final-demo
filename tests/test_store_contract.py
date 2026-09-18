"""Task 5 acceptance cases for ``app/store.py`` (TASKS.md item 5).

Item 5 ships the paste table: opening a store creates the ``pastes`` table and
the ``expires_at`` index, and the module offers ``insert``, ``get``,
``delete``, ``delete_expired``, ``count`` and ``text_bytes`` over one
connection in WAL mode, exercised against a temporary file including reopening
it (item 8's restart, item 6's reclamation).

These cases pin what the later tasks are allowed to rely on:

- opening the file is startup: exactly one table with the four documented
  columns and their declared types, exactly one index over ``expires_at``, a
  plain non-unique index because two pastes created at the same instant share
  a deadline (item 9);
- one connection, in WAL mode, serves every operation;
- ``get`` returns each row's own text and both stored instants, and a
  near-miss id is a miss rather than another paste's text (item 3);
- text goes in as data and comes back verbatim (item 2), the same text twice
  is two pastes (item 9), and the schema refuses a row without its NOT NULL
  values;
- an insert is committed as it happens, so the row is in the file for a
  process that never closed the store (item 8), and a store opened on that
  file hands back the stored deadline;
- ``delete`` reports the row it removed and does nothing on a repeat;
- ``delete_expired`` is inclusive at a fractional instant — gone at its
  deadline, kept one instant before it (item 4's boundary) — and rows and
  bytes return to their zero baseline once the deadlines have passed (item 6),
  including through a reopened file;
- ``close`` really closes the one connection, and the store offers no way to
  list or search pastes.

They complement ``tests/test_store.py`` (same task), which covers the raw
schema pragmas, the WAL flag on the file, a verbatim round-trip of newlines,
whitespace, non-ASCII and emoji, the unknown and malformed id misses, the
duplicate-id refusal, delete and sweep arithmetic, the byte-versus-character
measurement at the 1 MiB ceiling, 1000 round-trips under ``ids.new_id()``,
cross-process writes and eight threads at once.

Cases that need a route, a clock or the lifespan — the read path's
compare-delete-then-404, the create route's insert, the sweeper loop and the
configured database path — are recorded as untestable on this task in the
run's case list, so this file passes ``delete_expired`` an instant directly,
exactly as the sweeper will pass ``clock.now()``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from app import store as store_module
from app.store import Paste, Store

# One paste creation as the create route will perform it: the instant, the
# fixed three-hour lifetime and the stored deadline (ARCHITECTURE.md Create,
# PRD.md item 4).
CREATED_AT = 1_700_000_000.0
THREE_HOURS_IN_SECONDS = 3 * 60 * 60
DEADLINE = CREATED_AT + THREE_HOURS_IN_SECONDS

# ARCHITECTURE.md Data's four columns, as `(name, declared type, not-null,
# primary key)`. SQLite does not report a `TEXT PRIMARY KEY` as NOT NULL — the
# primary-key flag is what makes the id the key — so `id` has not-null 0 and
# the three columns the schema declares NOT NULL have 1.
DOCUMENTED_COLUMNS = [
    ("id", "TEXT", 0, 1),
    ("text", "TEXT", 1, 0),
    ("created_at", "REAL", 1, 0),
    ("expires_at", "REAL", 1, 0),
]

# The one index, and the one table, ARCHITECTURE.md Data declares.
DOCUMENTED_INDEX = "pastes_expires_at"
DOCUMENTED_TABLE = "pastes"

# The operations TASKS.md item 5 names, plus the lifecycle the lifespan needs
# (``close``) and the path the store was opened on.
DOCUMENTED_PUBLIC_SURFACE = {
    "insert",
    "get",
    "delete",
    "delete_expired",
    "count",
    "text_bytes",
    "close",
    "path",
}

# Ids chosen by the cases: the store stores the id it is handed and never
# invents or explores one (AGENTS.md Conventions: ids come from
# ``ids.new_id()``, which tests/test_ids_contract.py covers).
PASTE_ID = "A" * 22
OTHER_ID = "B" * 22
THIRD_ID = "C" * 22


class _RecordingSqlite:
    """Stands in for the ``sqlite3`` module and records every connection made.

    Installed in place of ``store.sqlite3`` to pin "one connection" in item 5:
    an implementation that opened a connection per call would record more than
    one. Everything else is delegated to the real module, so the store still
    opens a real database file.
    """

    def __init__(self) -> None:
        self.connections: list[sqlite3.Connection] = []

    def connect(self, *args: object, **kwargs: object) -> sqlite3.Connection:
        connection = sqlite3.connect(*args, **kwargs)  # type: ignore[arg-type]
        self.connections.append(connection)
        return connection

    def __getattr__(self, name: str) -> object:
        return getattr(sqlite3, name)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A throwaway file under ``tmp_path``, never the operator's database."""
    return tmp_path / "throwaway-pastes.db"


@pytest.fixture
def opened_store(db_path: Path) -> Iterator[Store]:
    """A store on the throwaway file, closed even when a case fails."""
    opened = Store(db_path)
    try:
        yield opened
    finally:
        opened.close()


def _plain_connection(db_path: Path) -> sqlite3.Connection:
    """A second connection to the file, for inspecting it from outside the store."""
    return sqlite3.connect(db_path)


def _tables(db_path: Path) -> set[str]:
    """The user tables in the file (SQLite's own bookkeeping is an index)."""
    connection = _plain_connection(db_path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _columns(db_path: Path) -> list[tuple[str, str, int, int]]:
    """The ``pastes`` columns as ``(name, declared type, not-null, primary key)``."""
    connection = _plain_connection(db_path)
    try:
        rows = connection.execute(f"PRAGMA table_info({DOCUMENTED_TABLE})").fetchall()
    finally:
        connection.close()
    return [(row[1], row[2], row[3], row[5]) for row in rows]


def _index_columns(db_path: Path, index_name: str) -> list[str]:
    """The columns an index covers, in order."""
    connection = _plain_connection(db_path)
    try:
        rows = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    finally:
        connection.close()
    return [row[2] for row in rows]


def _index_flags(db_path: Path, index_name: str) -> dict[str, int | str]:
    """An index's ``(unique, origin, partial)`` flags, from the table's index list."""
    connection = _plain_connection(db_path)
    try:
        rows = connection.execute(
            f"PRAGMA index_list({DOCUMENTED_TABLE})"
        ).fetchall()
    finally:
        connection.close()

    # index_list rows are (seq, name, unique, origin, partial).
    for row in rows:
        if row[1] == index_name:
            return {"unique": int(row[2]), "origin": row[3], "partial": int(row[4])}
    raise AssertionError(
        f"{index_name} is not an index of {DOCUMENTED_TABLE}: "
        f"saw {[row[1] for row in rows]}"
    )


def test_tc001_opening_the_store_creates_the_pastes_table_with_the_documented_columns(
    db_path: Path,
) -> None:
    """TASKS.md item 5: startup creates the table, and only that table (item 5)."""
    assert not db_path.exists()

    opened = Store(db_path)
    try:
        assert db_path.is_file()
        assert _tables(db_path) == {DOCUMENTED_TABLE}
        assert _columns(db_path) == DOCUMENTED_COLUMNS
        assert opened.path == db_path
    finally:
        opened.close()


def test_tc002_the_expires_at_index_covers_exactly_expires_at_and_is_not_unique(
    opened_store: Store, db_path: Path
) -> None:
    """The index the sweep's range scan uses, and nothing more (ARCHITECTURE.md Data)."""
    assert _index_columns(db_path, DOCUMENTED_INDEX) == ["expires_at"]
    assert _index_flags(db_path, DOCUMENTED_INDEX) == {
        "unique": 0,
        "origin": "c",
        "partial": 0,
    }


def test_tc003_every_operation_uses_the_one_connection_and_it_is_in_wal_mode(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    """TASKS.md item 5: one WAL connection for the whole store, closed once."""
    recording = _RecordingSqlite()
    monkeypatch.setattr(store_module, "sqlite3", recording)

    opened = Store(db_path)
    try:
        (connection,) = recording.connections
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

        opened.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)
        opened.get(PASTE_ID)
        opened.get(OTHER_ID)
        opened.delete(OTHER_ID)
        opened.delete_expired(DEADLINE - THREE_HOURS_IN_SECONDS)
        opened.count()
        opened.text_bytes()
    finally:
        opened.close()

    assert len(recording.connections) == 1


def test_tc004_get_returns_each_pastes_own_text_and_instants(opened_store: Store) -> None:
    """The read path's lookup: one row's text and deadline per id (item 5, item 8)."""
    first_text = "first\npaste\n"
    second_created = CREATED_AT + 1.5
    third_created = CREATED_AT + 60.0
    third_text = "  third paste 😀  "

    opened_store.insert(PASTE_ID, first_text, CREATED_AT, DEADLINE)
    opened_store.insert(
        OTHER_ID,
        "second paste",
        second_created,
        second_created + THREE_HOURS_IN_SECONDS,
    )
    opened_store.insert(
        THIRD_ID,
        third_text,
        third_created,
        third_created + THREE_HOURS_IN_SECONDS,
    )

    first = opened_store.get(PASTE_ID)
    second = opened_store.get(OTHER_ID)
    third = opened_store.get(THIRD_ID)

    assert first is not None
    assert second is not None
    assert third is not None

    assert first == Paste(
        id=PASTE_ID, text=first_text, created_at=CREATED_AT, expires_at=DEADLINE
    )
    assert (first.id, first.text, first.created_at, first.expires_at) == (
        PASTE_ID,
        first_text,
        CREATED_AT,
        DEADLINE,
    )
    assert second == Paste(
        id=OTHER_ID,
        text="second paste",
        created_at=second_created,
        expires_at=second_created + THREE_HOURS_IN_SECONDS,
    )
    assert third == Paste(
        id=THIRD_ID,
        text=third_text,
        created_at=third_created,
        expires_at=third_created + THREE_HOURS_IN_SECONDS,
    )


def test_tc005_text_that_reads_like_sql_is_stored_as_data(
    opened_store: Store, db_path: Path
) -> None:
    """Stored text is a value, not a statement: the paste survives it verbatim."""
    hostile = "'); DROP TABLE pastes;--"

    opened_store.insert(PASTE_ID, hostile, CREATED_AT, DEADLINE)

    stored = opened_store.get(PASTE_ID)
    assert stored is not None
    assert stored.text == hostile
    assert opened_store.count() == 1
    assert _tables(db_path) == {DOCUMENTED_TABLE}
    assert _columns(db_path) == DOCUMENTED_COLUMNS


def test_tc006_a_near_miss_id_finds_nothing_and_never_the_stored_text(
    opened_store: Store,
) -> None:
    """PRD.md item 3: a wrong but well-formed id must not reach someone else's paste."""
    opened_store.insert(PASTE_ID, "the stored text", CREATED_AT, DEADLINE)

    near_misses = [
        "a" * 22,  # right shape, wrong case
        "A" * 21,  # one character short
        "A" * 23,  # one character long
        PASTE_ID + " ",  # trailing whitespace
        " " + PASTE_ID,  # leading whitespace
        "A" * 11 + "B" * 11,  # one character different
        OTHER_ID,  # a well-formed id that was never issued
        "no-such-id",  # a malformed id
    ]

    for candidate in near_misses:
        assert opened_store.get(candidate) is None, candidate


def test_tc007_an_insert_is_committed_for_an_independent_reader(
    opened_store: Store, db_path: Path
) -> None:
    """PRD.md item 8: the row is in the file without the store being closed."""
    opened_store.insert(PASTE_ID, "committed at once", CREATED_AT, DEADLINE)

    connection = _plain_connection(db_path)
    try:
        rows = connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes WHERE id = ?",
            (PASTE_ID,),
        ).fetchall()
    finally:
        connection.close()

    assert rows == [(PASTE_ID, "committed at once", CREATED_AT, DEADLINE)]


def test_tc008_a_second_store_sees_the_paste_while_the_first_stays_open(
    db_path: Path,
) -> None:
    """A restart that never closed the file still hands every paste back (item 8)."""
    text = "written before the crash ☃\n"

    first = Store(db_path)
    try:
        first.insert(PASTE_ID, text, CREATED_AT, DEADLINE)

        second = Store(db_path)
        try:
            stored = second.get(PASTE_ID)

            assert stored == Paste(
                id=PASTE_ID, text=text, created_at=CREATED_AT, expires_at=DEADLINE
            )
            assert second.count() == 1
            assert second.text_bytes() == len(text.encode("utf-8"))
        finally:
            second.close()

        assert first.get(PASTE_ID) is not None
    finally:
        first.close()


def test_tc009_the_schema_refuses_a_row_without_its_not_null_values(
    opened_store: Store,
) -> None:
    """ARCHITECTURE.md Data declares text, created_at and expires_at NOT NULL."""
    with pytest.raises(sqlite3.IntegrityError):
        opened_store.insert(PASTE_ID, None, CREATED_AT, DEADLINE)  # type: ignore[arg-type]

    with pytest.raises(sqlite3.IntegrityError):
        opened_store.insert(PASTE_ID, "text", None, DEADLINE)  # type: ignore[arg-type]

    with pytest.raises(sqlite3.IntegrityError):
        opened_store.insert(PASTE_ID, "text", CREATED_AT, None)  # type: ignore[arg-type]

    assert opened_store.get(PASTE_ID) is None
    assert opened_store.count() == 0
    assert opened_store.text_bytes() == 0


def test_tc010_deleting_the_same_id_twice_removes_one_row_the_first_time(
    opened_store: Store,
) -> None:
    """The read path deletes an expired row once; a repeat has nothing left to do."""
    opened_store.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)

    assert opened_store.delete(PASTE_ID) == 1
    assert opened_store.delete(PASTE_ID) == 0
    assert opened_store.count() == 0
    assert opened_store.text_bytes() == 0


def test_tc011_the_sweep_boundary_is_inclusive_at_a_fractional_instant(
    opened_store: Store,
) -> None:
    """Deadlines are floats and a paste is gone from its deadline on (item 4)."""
    now = 1_700_003_600.75
    one_instant_later = 1_700_003_600.8

    opened_store.insert(
        PASTE_ID, "dies at the instant", now - THREE_HOURS_IN_SECONDS, now
    )
    opened_store.insert(
        OTHER_ID,
        "lives one instant longer",
        one_instant_later - THREE_HOURS_IN_SECONDS,
        one_instant_later,
    )

    assert opened_store.delete_expired(now) == 1
    assert opened_store.get(PASTE_ID) is None
    assert opened_store.get(OTHER_ID) is not None
    assert opened_store.count() == 1


def test_tc012_the_same_text_stored_twice_is_two_rows_and_two_texts_of_bytes(
    opened_store: Store,
) -> None:
    """PRD.md item 9: no deduplication, so identical submissions are separate pastes."""
    text = "identical text"

    opened_store.insert(PASTE_ID, text, CREATED_AT, DEADLINE)
    opened_store.insert(OTHER_ID, text, CREATED_AT, DEADLINE)

    first = opened_store.get(PASTE_ID)
    second = opened_store.get(OTHER_ID)

    assert opened_store.count() == 2
    assert first is not None
    assert second is not None
    assert first.text == second.text == text
    assert opened_store.text_bytes() == 2 * len(text.encode("utf-8"))


def test_tc013_two_pastes_may_share_one_expires_at(opened_store: Store) -> None:
    """Two pastes created in the same instant keep their own, identical deadline."""
    opened_store.insert(PASTE_ID, "first", CREATED_AT, DEADLINE)
    opened_store.insert(OTHER_ID, "second", CREATED_AT, DEADLINE)

    first = opened_store.get(PASTE_ID)
    second = opened_store.get(OTHER_ID)

    assert opened_store.count() == 2
    assert first is not None
    assert second is not None
    assert first.text == "first"
    assert second.text == "second"
    assert first.expires_at == second.expires_at == DEADLINE
    assert opened_store.delete_expired(DEADLINE) == 2


def test_tc014_a_reopened_store_still_enforces_the_stored_deadlines(db_path: Path) -> None:
    """Items 6 and 8: nothing reclaimed early, everything reclaimed at the deadline."""
    pastes = 10
    text = "x" * 100

    first = Store(db_path)
    try:
        for index in range(pastes):
            first.insert(f"{index:0>22}", text, CREATED_AT, DEADLINE)
    finally:
        first.close()

    reopened = Store(db_path)
    try:
        assert reopened.count() == pastes
        assert reopened.text_bytes() == pastes * len(text.encode("utf-8"))

        assert reopened.delete_expired(DEADLINE - 1) == 0
        assert reopened.count() == pastes
        assert reopened.text_bytes() == pastes * len(text.encode("utf-8"))

        assert reopened.delete_expired(DEADLINE) == pastes
        assert reopened.count() == 0
        assert reopened.text_bytes() == 0
    finally:
        reopened.close()


def test_tc015_close_closes_the_one_connection_and_frees_the_file(db_path: Path) -> None:
    """Shutdown releases the connection; the committed row stays in the file."""
    opened = Store(db_path)
    opened.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)
    opened.close()

    operations = [
        lambda: opened.insert(OTHER_ID, "text", CREATED_AT, DEADLINE),
        lambda: opened.get(PASTE_ID),
        lambda: opened.delete(PASTE_ID),
        lambda: opened.delete_expired(DEADLINE),
        lambda: opened.count(),
        lambda: opened.text_bytes(),
    ]
    for operation in operations:
        with pytest.raises(sqlite3.ProgrammingError):
            operation()

    reopened = Store(db_path)
    try:
        stored = reopened.get(PASTE_ID)
        assert stored is not None
        assert stored.text == "text"
    finally:
        reopened.close()


def test_tc016_the_store_ships_the_documented_operations_and_nothing_to_list_pastes() -> None:
    """TASKS.md item 5's list, and no capability that would enumerate pastes."""
    public_surface = {name for name in vars(Store) if not name.startswith("_")}

    assert public_surface == DOCUMENTED_PUBLIC_SURFACE
    for name in ("insert", "get", "delete", "delete_expired", "count", "text_bytes"):
        assert callable(getattr(Store, name)), name
