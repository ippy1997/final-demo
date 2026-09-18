"""Task 5: ``app/store.py``, the paste table over one WAL connection
(TASKS.md item 5).

The module ships the ``pastes`` table and the ``expires_at`` index created at
startup, with ``insert``, ``get``, ``delete``, ``delete_expired``, ``count``
and ``text_bytes`` over one connection, and the task names its criterion: the
store is exercised against a temporary file, including reopening it. That
reopening is PRD.md item 8 — an unexpired paste is still there after the
service is rebuilt on the same file, with the deadline it was created with —
and the two measurements, rows (``count``) and text bytes (``text_bytes``),
are what PRD.md item 6's reclamation is checked against (TASKS.md item 9).

Every case below therefore runs against a throwaway file under ``tmp_path``
and never touches the operator's database (PRD.md Success, AGENTS.md
Conventions). What they pin:

- the file is created where it was asked for, holding the ``pastes`` table
  with the four documented columns and the one index over ``expires_at``, and
  the connection reports WAL;
- exactly one connection is opened and reused by every operation, and the
  store is usable from several threads at once;
- a paste round-trips verbatim with both instants unchanged, and an unknown id
  returns ``None`` rather than raising;
- ``delete`` removes one row and reports it, leaving other pastes alone;
- ``delete_expired`` removes rows at or past their deadline and nothing else,
  the boundary PRD.md's defaults fix;
- ``count`` and ``text_bytes`` measure rows and stored UTF-8 bytes, and both
  return to their earlier values once expired rows are swept;
- reopening the same file gives the pastes back with their stored deadlines,
  and a store opened on an existing file adds nothing and loses nothing.

Cases that need a clock, a route or the sweeper — the read path's
"compare, delete, then 404", the create route's insert, and the loop that
calls ``delete_expired`` — belong to TASKS.md items 6, 7 and 9, so this file
passes ``delete_expired`` an instant directly, exactly as the sweeper will
pass ``clock.now()``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterator

import pytest

from app import ids
from app import store as store_module
from app.store import Paste, Store

# One paste creation, as the create route will perform it: an instant, the
# fixed three-hour lifetime, and the stored deadline
# (ARCHITECTURE.md Create; PRD.md item 4).
CREATED_AT = 1_700_000_000.0
THREE_HOURS_IN_SECONDS = 3 * 60 * 60
DEADLINE = CREATED_AT + THREE_HOURS_IN_SECONDS

# The documented columns, in the order ARCHITECTURE.md Data declares them:
# `(name, declared type, not-null flag, primary-key flag)`. SQLite does not
# treat a `TEXT PRIMARY KEY` as implicitly NOT NULL — the primary-key flag is
# what makes `id` the key — so the pragma reports not-null 0 for it and 1 for
# the three columns the schema declares NOT NULL.
DOCUMENTED_COLUMNS = [
    ("id", "TEXT", 0, 1),
    ("text", "TEXT", 1, 0),
    ("created_at", "REAL", 1, 0),
    ("expires_at", "REAL", 1, 0),
]

# The index the sweeper's range scan relies on (ARCHITECTURE.md Data).
EXPIRES_AT_INDEX = "pastes_expires_at"

# Ids the cases here choose themselves: the store takes the id it is given and
# never invents one (AGENTS.md Conventions: ids come from ``ids.new_id()``).
PASTE_ID = "A" * 22
OTHER_ID = "B" * 22
UNKNOWN_ID = "Z" * 22

# The draws the round-trip case makes at PRD.md item 3's own scale.
BACK_TO_BACK_IDS = 1000


class _CountingSqlite:
    """Stands in for the ``sqlite3`` module and records the connections opened.

    Installed in place of ``store.sqlite3`` to pin "one WAL connection"
    (ARCHITECTURE.md Stack and its decision row): a store that opened a fresh
    connection per call would record more than one. Everything else is
    delegated to the real module, so the store still opens a real database.
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
    """A throwaway database file, never the operator's (PRD.md Success)."""
    return tmp_path / "throwaway-pastes.db"


@pytest.fixture
def opened_store(db_path: Path) -> Iterator[Store]:
    """A store on the throwaway file, closed even when a case fails."""
    opened = Store(db_path)
    try:
        yield opened
    finally:
        opened.close()


def _connection(db_path: Path) -> sqlite3.Connection:
    """A second connection to the file, for reading the schema back."""
    return sqlite3.connect(db_path)


def _columns(db_path: Path) -> list[tuple[str, str, int, int]]:
    """The ``pastes`` columns as ``(name, type, not-null, primary key)``."""
    connection = _connection(db_path)
    try:
        rows = connection.execute("PRAGMA table_info(pastes)").fetchall()
    finally:
        connection.close()
    return [(row[1], row[2], row[3], row[5]) for row in rows]


def _object_names(db_path: Path) -> set[str]:
    """Every table and index in the file."""
    connection = _connection(db_path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _index_columns(db_path: Path, index_name: str) -> list[str]:
    """The columns an index covers, in order."""
    connection = _connection(db_path)
    try:
        rows = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    finally:
        connection.close()
    return [row[2] for row in rows]


def _journal_mode(db_path: Path) -> str:
    """What a fresh connection to the file reports as its journal mode."""
    connection = _connection(db_path)
    try:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()


def _sweep_plan(db_path: Path) -> str:
    """SQLite's plan for the statement the sweeper runs, as one string."""
    connection = _connection(db_path)
    try:
        rows = connection.execute(
            "EXPLAIN QUERY PLAN DELETE FROM pastes WHERE expires_at <= ?",
            (DEADLINE,),
        ).fetchall()
    finally:
        connection.close()
    return " | ".join(str(row[-1]) for row in rows)


def test_opening_the_store_creates_the_database_file_where_it_was_asked_for(
    db_path: Path,
) -> None:
    """The store owns creating the file; config only resolves the path (item 5)."""
    assert not db_path.exists()

    opened = Store(db_path)
    try:
        assert db_path.is_file()
        assert opened.path == db_path
    finally:
        opened.close()


def test_opening_the_store_creates_the_pastes_table_with_the_documented_columns(
    opened_store: Store, db_path: Path
) -> None:
    """ARCHITECTURE.md Data's four columns: id, text, created_at, expires_at."""
    assert "pastes" in _object_names(db_path)
    assert _columns(db_path) == DOCUMENTED_COLUMNS


def test_opening_the_store_creates_the_expires_at_index(
    opened_store: Store, db_path: Path
) -> None:
    """The one index, over ``expires_at``, for the sweep's range scan."""
    assert EXPIRES_AT_INDEX in _object_names(db_path)
    assert _index_columns(db_path, EXPIRES_AT_INDEX) == ["expires_at"]


def test_the_sweepers_delete_statement_is_served_by_that_index(
    opened_store: Store, db_path: Path
) -> None:
    """The index is not decorative: the sweep is a search, not a table scan."""
    plan = _sweep_plan(db_path)

    assert EXPIRES_AT_INDEX in plan
    assert plan.upper().startswith("SEARCH")


def test_the_database_file_is_in_wal_journal_mode(
    opened_store: Store, db_path: Path
) -> None:
    """WAL is a property of the file: a fresh connection reports it too."""
    assert _journal_mode(db_path) == "wal"


def test_the_store_keeps_one_connection_for_every_operation(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    """One connection, not one per call (ARCHITECTURE.md Stack decision row)."""
    counting = _CountingSqlite()
    monkeypatch.setattr(store_module, "sqlite3", counting)

    opened = Store(db_path)
    try:
        opened.insert(PASTE_ID, "hello", CREATED_AT, DEADLINE)
        opened.get(PASTE_ID)
        opened.get(UNKNOWN_ID)
        opened.count()
        opened.text_bytes()
        opened.delete(UNKNOWN_ID)
        opened.delete_expired(DEADLINE)
    finally:
        opened.close()

    assert len(counting.connections) == 1


def test_that_one_connection_is_in_wal_mode(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    """The connection the store uses is the WAL one, not just the file flag."""
    counting = _CountingSqlite()
    monkeypatch.setattr(store_module, "sqlite3", counting)

    opened = Store(db_path)
    try:
        (connection,) = counting.connections
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        assert mode == "wal"
        assert opened.count() == 0
    finally:
        opened.close()


def test_insert_then_get_returns_the_paste_exactly_as_it_was_stored(
    opened_store: Store,
) -> None:
    """PRD.md item 2: newlines, outer whitespace, non-ASCII and emoji survive."""
    text = "  leading\nline\r\n\ttabbed\n  é — snowman ☃ 😀  trailing  \n"

    opened_store.insert(PASTE_ID, text, CREATED_AT, DEADLINE)

    assert opened_store.get(PASTE_ID) == Paste(
        id=PASTE_ID, text=text, created_at=CREATED_AT, expires_at=DEADLINE
    )


def test_the_stored_instants_are_handed_back_unchanged(opened_store: Store) -> None:
    """The deadline is the instant that went in: nothing recomputes it (item 4)."""
    created_at = CREATED_AT + 0.5
    expires_at = created_at + THREE_HOURS_IN_SECONDS

    opened_store.insert(PASTE_ID, "text", created_at, expires_at)

    stored = opened_store.get(PASTE_ID)
    assert stored is not None
    assert stored.created_at == created_at
    assert stored.expires_at == expires_at
    assert stored.expires_at - stored.created_at == THREE_HOURS_IN_SECONDS


def test_get_of_an_unknown_id_returns_none(opened_store: Store) -> None:
    """An id never issued is a miss, not an error: the read path turns it into 404."""
    opened_store.insert(PASTE_ID, "someone else's text", CREATED_AT, DEADLINE)

    assert opened_store.get(OTHER_ID) is None
    assert opened_store.get(UNKNOWN_ID) is None


def test_get_of_a_malformed_id_returns_none(opened_store: Store) -> None:
    """A well-formed miss and a nonsense id are the same nothing (PRD.md item 5)."""
    opened_store.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)

    assert opened_store.get("no-such-id") is None
    assert opened_store.get("") is None


def test_a_second_insert_under_the_same_id_is_refused(opened_store: Store) -> None:
    """The primary key refuses a replacement: a stored paste is not overwritten."""
    opened_store.insert(PASTE_ID, "first", CREATED_AT, DEADLINE)

    with pytest.raises(sqlite3.IntegrityError):
        opened_store.insert(PASTE_ID, "second", CREATED_AT, DEADLINE)

    stored = opened_store.get(PASTE_ID)
    assert stored is not None
    assert stored.text == "first"
    assert opened_store.count() == 1


def test_delete_removes_the_row_and_reports_it(opened_store: Store) -> None:
    """The read path's deletion of an expired paste (ARCHITECTURE.md Read)."""
    opened_store.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)

    assert opened_store.delete(PASTE_ID) == 1
    assert opened_store.get(PASTE_ID) is None
    assert opened_store.count() == 0


def test_delete_of_an_unknown_id_removes_nothing(opened_store: Store) -> None:
    opened_store.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)

    assert opened_store.delete(OTHER_ID) == 0
    assert opened_store.count() == 1


def test_delete_leaves_every_other_paste_alone(opened_store: Store) -> None:
    """PRD.md item 5: an unknown or expired id must never disturb another paste."""
    opened_store.insert(PASTE_ID, "keep me", CREATED_AT, DEADLINE)
    opened_store.insert(OTHER_ID, "delete me", CREATED_AT, DEADLINE)

    opened_store.delete(OTHER_ID)

    assert opened_store.get(PASTE_ID) is not None
    assert opened_store.count() == 1


def test_delete_expired_removes_only_pastes_at_or_past_their_deadline(
    opened_store: Store,
) -> None:
    """The stored boundary: still alive at deadline - 1s, gone at the deadline."""
    opened_store.insert("1" * 22, "already gone", DEADLINE - 5, DEADLINE - 1)
    opened_store.insert("2" * 22, "just gone", DEADLINE - 1, DEADLINE)
    opened_store.insert("3" * 22, "still alive", DEADLINE - 1, DEADLINE + 1)

    deleted = opened_store.delete_expired(DEADLINE)

    assert deleted == 2
    assert opened_store.get("1" * 22) is None
    assert opened_store.get("2" * 22) is None
    assert opened_store.get("3" * 22) is not None
    assert opened_store.count() == 1


def test_delete_expired_changes_nothing_when_no_deadline_has_passed(
    opened_store: Store,
) -> None:
    opened_store.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)

    assert opened_store.delete_expired(DEADLINE - 1) == 0
    assert opened_store.count() == 1
    assert opened_store.get(PASTE_ID) is not None


def test_delete_expired_on_an_empty_store_removes_nothing(opened_store: Store) -> None:
    """The sweeper's first tick on a fresh file."""
    assert opened_store.delete_expired(DEADLINE) == 0
    assert opened_store.count() == 0


def test_repeated_sweeps_are_idempotent(opened_store: Store) -> None:
    """A second tick over the same instant finds nothing left to delete."""
    opened_store.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)

    assert opened_store.delete_expired(DEADLINE) == 1
    assert opened_store.delete_expired(DEADLINE) == 0
    assert opened_store.delete_expired(DEADLINE + THREE_HOURS_IN_SECONDS) == 0


def test_count_and_text_bytes_start_at_zero_for_a_new_store(
    opened_store: Store,
) -> None:
    """An empty table has no rows and no bytes, not an indeterminate sum."""
    assert opened_store.count() == 0
    assert opened_store.text_bytes() == 0


def test_count_counts_every_stored_row(opened_store: Store) -> None:
    for index in range(5):
        opened_store.insert(f"{index:0>22}", f"text {index}", CREATED_AT, DEADLINE)

    assert opened_store.count() == 5


def test_text_bytes_measures_stored_text_in_utf8_bytes_not_characters(
    opened_store: Store,
) -> None:
    """PRD.md item 6 counts the text that is stored, as the disk holds it."""
    stored = {
        "1" * 22: "abc",  # 3 bytes, 3 characters
        "2" * 22: "é",  # 2 bytes, 1 character
        "3" * 22: "😀",  # 4 bytes, 1 character
        "4" * 22: "",  # no bytes, no characters
    }
    for identifier, text in stored.items():
        opened_store.insert(identifier, text, CREATED_AT, DEADLINE)

    assert opened_store.text_bytes() == 3 + 2 + 4 + 0
    assert opened_store.text_bytes() != sum(len(text) for text in stored.values())


def test_text_bytes_measures_large_pastes_by_their_byte_length(
    opened_store: Store,
) -> None:
    """A paste near the 1 MiB ceiling is counted in full (PRD.md item 7's unit)."""
    body = "l" * (1024 * 1024)

    opened_store.insert(PASTE_ID, body, CREATED_AT, DEADLINE)

    assert opened_store.text_bytes() == len(body.encode("utf-8")) == 1024 * 1024


def test_count_and_text_bytes_return_to_their_earlier_values_after_a_sweep(
    opened_store: Store,
) -> None:
    """PRD.md item 6: rows and bytes are reclaimed, not merely hidden."""
    opened_store.insert("K" * 22, "kept", CREATED_AT, DEADLINE + 1)
    rows_before = opened_store.count()
    bytes_before = opened_store.text_bytes()

    for index in range(20):
        opened_store.insert(
            f"{index:0>22}", "x" * 500, CREATED_AT, CREATED_AT + THREE_HOURS_IN_SECONDS
        )
    assert opened_store.count() > rows_before
    assert opened_store.text_bytes() > bytes_before

    opened_store.delete_expired(DEADLINE)

    assert opened_store.count() == rows_before
    assert opened_store.text_bytes() == bytes_before


def test_reopening_the_file_returns_the_paste_with_its_stored_deadline(
    db_path: Path,
) -> None:
    """PRD.md item 8: a paste inside its three hours survives a restart."""
    text = "a paste that outlives the process\nsecond line\n"

    opened = Store(db_path)
    try:
        opened.insert(PASTE_ID, text, CREATED_AT, DEADLINE)
    finally:
        opened.close()

    reopened = Store(db_path)
    try:
        stored = reopened.get(PASTE_ID)

        assert stored == Paste(
            id=PASTE_ID, text=text, created_at=CREATED_AT, expires_at=DEADLINE
        )
        assert reopened.count() == 1
        assert reopened.text_bytes() == len(text.encode("utf-8"))
    finally:
        reopened.close()


def test_reopening_the_file_after_a_deadline_passed_still_reads_the_row(
    db_path: Path,
) -> None:
    """Downtime does not delete anything by itself: the sweeper or a read does.

    The row is there with the deadline that fell during the downtime, so the
    reopened store's ``delete_expired`` collects it — the shape of TASKS.md
    item 12's restart case, decided by the stored instant rather than by how
    long the process was away (PRD.md item 4).
    """
    opened = Store(db_path)
    try:
        opened.insert(PASTE_ID, "died during the downtime", CREATED_AT, DEADLINE)
    finally:
        opened.close()

    reopened = Store(db_path)
    try:
        assert reopened.get(PASTE_ID) is not None

        assert reopened.delete_expired(DEADLINE) == 1

        assert reopened.get(PASTE_ID) is None
        assert reopened.count() == 0
        assert reopened.text_bytes() == 0
    finally:
        reopened.close()


def test_reopening_an_existing_file_adds_no_second_table_or_index(db_path: Path) -> None:
    """The schema statements are ``IF NOT EXISTS``, so startup is repeatable."""
    first = Store(db_path)
    try:
        first.insert(PASTE_ID, "text", CREATED_AT, DEADLINE)
    finally:
        first.close()

    names_before = _object_names(db_path)
    second = Store(db_path)
    try:
        assert _object_names(db_path) == names_before
        assert "pastes" in names_before
        assert EXPIRES_AT_INDEX in names_before
        assert second.get(PASTE_ID) is not None
    finally:
        second.close()

    assert _columns(db_path) == DOCUMENTED_COLUMNS
    assert _index_columns(db_path, EXPIRES_AT_INDEX) == ["expires_at"]
    assert _journal_mode(db_path) == "wal"


def test_a_paste_written_by_a_reopened_store_is_readable_by_the_next_one(
    db_path: Path,
) -> None:
    """Writes are committed, so each process hands the next one a full file."""
    first = Store(db_path)
    try:
        first.insert(PASTE_ID, "from the first process", CREATED_AT, DEADLINE)
    finally:
        first.close()

    second = Store(db_path)
    try:
        second.insert(OTHER_ID, "from the second process", CREATED_AT, DEADLINE)
    finally:
        second.close()

    third = Store(db_path)
    try:
        assert third.count() == 2

        first_paste = third.get(PASTE_ID)
        second_paste = third.get(OTHER_ID)

        assert first_paste is not None
        assert first_paste.text == "from the first process"
        assert second_paste is not None
        assert second_paste.text == "from the second process"
    finally:
        third.close()


def test_one_thousand_pastes_round_trip_under_ids_from_the_id_generator(
    opened_store: Store,
) -> None:
    """The scale PRD.md item 3 states its check over, through the store itself."""
    drawn = [ids.new_id() for _ in range(BACK_TO_BACK_IDS)]
    for index, identifier in enumerate(drawn):
        opened_store.insert(identifier, f"paste {index}", CREATED_AT, DEADLINE)

    assert opened_store.count() == BACK_TO_BACK_IDS

    read_back = []
    for identifier in drawn:
        stored = opened_store.get(identifier)
        assert stored is not None
        read_back.append(stored.text)

    assert len(set(read_back)) == BACK_TO_BACK_IDS
    assert set(read_back) == {f"paste {index}" for index in range(BACK_TO_BACK_IDS)}


def test_the_store_is_usable_from_several_threads_at_once(opened_store: Store) -> None:
    """One connection shared by FastAPI's threadpool and the sweeper (item 5).

    Writes from eight threads at once all land, every paste reads back, and the
    counters agree with what was inserted: the store serialises its own use of
    the single connection.
    """
    threads = 8
    per_thread = 25
    failures: list[BaseException] = []

    def write(worker: int) -> None:
        try:
            for index in range(per_thread):
                identifier = f"{worker:0>2}-{index:0>19}"
                opened_store.insert(
                    identifier, f"worker {worker} paste {index}", CREATED_AT, DEADLINE
                )
                assert opened_store.get(identifier) is not None
                opened_store.count()
                opened_store.text_bytes()
        except BaseException as error:  # pragma: no cover - reported below
            failures.append(error)

    workers = [
        threading.Thread(target=write, args=(worker,)) for worker in range(threads)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert failures == []
    assert opened_store.count() == threads * per_thread
    assert opened_store.text_bytes() == sum(
        len(f"worker {worker} paste {index}".encode("utf-8"))
        for worker in range(threads)
        for index in range(per_thread)
    )
