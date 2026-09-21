"""Burn-after-read at the edges its first cases do not pin.

``tests/test_burn_after_read.py`` walks the feature from the caller's side:
the flag is opt-in, a flagged create stores the flag and keeps the documented
three-field response, a flagged paste is served once and then answers the
uniform 404, an unread flagged paste still expires at its stored deadline, the
flag survives reopening the database, and a database created before the column
existed is migrated with its old rows left ordinary. This module adds the
edges those cases leave open:

- BURN-001: a flagged paste read one second before its deadline is served and
  consumed at the boundary, so the same link is the uniform 404 even though
  the deadline has not yet arrived (PRD.md item 4's boundary, and "delete on
  first successful GET").
- BURN-002: the explicit false value of the boolean flag is the ordinary,
  never-consume behaviour, exactly like omitting it.
- BURN-003a/b/c: opting in cannot turn a rejected create into a stored paste —
  an empty body, an oversized body and an invalid UTF-8 body are the same 400
  or 413 and store nothing (PRD.md item 7).
- BURN-004: a value that is not a boolean is rejected by the framework's query
  validation with 422 and stores nothing, so a malformed flag is never
  silently treated as true or false.
- BURN-005: the deletion behind a flagged paste is atomic: twelve racing
  ``consume`` calls on the same paste return the text to exactly one caller and
  ``None`` to the other eleven, so a racing crawler or previewer cannot also
  receive the text (app/store.py, ``consume``).
- BURN-006: an unread flagged paste is kept until its deadline and is then
  collected by ``delete_expired``, not only by a read — burn-after-read
  shortens a read paste, not its stored lifetime.
- BURN-007: migrating a pre-flag database keeps an old row's stored
  ``created_at`` and ``expires_at`` instants, adding the flag as false rather
  than rebuilding the row and moving its deadline.

The route-level cases create through ``POST /pastes`` and read through the
link the create response returned; the store-level cases use the real ``Store``
on the fixture's throwaway file, the same way the neighbouring files do. The
clock is the fixture's ``FakeClock`` throughout, so expiry and consumption are
observed by moving the clock rather than by waiting (PRD.md Success).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config
from app.store import Paste, Store

# The instant the route-level cases create at, and the deadline the create
# route derives from it (PRD.md item 4).
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The documented path, content type and error contract (ARCHITECTURE.md Routes
# and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# An id in the documented shape that was never issued (PRD.md item 3).
UNKNOWN_ID = "Z" * 22

# The query strings for the three values of the opt-in flag exercised here.
BURN_AFTER_READ_TRUE_QUERY = "?burn_after_read=true"
BURN_AFTER_READ_FALSE_QUERY = "?burn_after_read=false"
BURN_AFTER_READ_INVALID_QUERY = "?burn_after_read=not-a-bool"

# The ceiling the rejected-create cases work at (PRD.md item 7).
MAX_BYTES = config.MAX_PASTE_BYTES


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float, int]]:
    """Every stored paste as ``(id, text, created_at, expires_at, flag)``.

    Read through a second connection to the throwaway file the app was started
    on, so "the row is gone after the first read" and "the flag is stored" are
    measured on the database rather than inferred from a response.
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at, burn_after_read FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned."""
    return urlsplit(paste["url"]).path


def _created_paste(
    client: TestClient,
    fake_clock: FakeClock,
    text: str,
    *,
    query: str,
) -> dict[str, str]:
    """Post ``text`` at the case's creation instant and return the create body."""
    fake_clock.instant = CREATED_AT
    response = client.post(PASTES_PATH + query, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def test_a_burn_after_read_paste_read_one_second_before_its_deadline_is_still_consumed(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """BURN-001: the first successful read consumes it, at the expiry boundary."""
    text = "read at the very edge, then gone\n"
    paste = _created_paste(
        client, fake_clock, text, query=BURN_AFTER_READ_TRUE_QUERY
    )

    fake_clock.instant = DEADLINE - 1
    first = client.get(_paste_path(paste))

    assert first.status_code == 200
    assert first.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert first.content == text.encode("utf-8")
    assert _stored_rows(db_path) == []

    # Consumed while still before the deadline: the same link is now the
    # uniform 404 an id that was never issued gets (PRD.md item 5).
    second = client.get(_paste_path(paste))
    unknown = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert second.status_code == 404
    assert second.content == NOT_FOUND_BODY
    assert second.json() == {"error": NOT_FOUND_CODE}
    assert second.content == unknown.content
    assert dict(second.headers) == dict(unknown.headers)
    assert text.encode("utf-8") not in second.content

    # The deadline itself changes nothing: the row is already gone.
    fake_clock.instant = DEADLINE
    later = client.get(_paste_path(paste))
    assert later.status_code == 404
    assert later.content == NOT_FOUND_BODY


def test_an_explicit_false_burn_after_read_flag_is_an_ordinary_paste(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """BURN-002: the false value opts out, so repeated reads do not consume it."""
    text = "explicitly not burn after read\n"
    paste = _created_paste(
        client, fake_clock, text, query=BURN_AFTER_READ_FALSE_QUERY
    )

    assert _stored_rows(db_path) == [
        (paste["id"], text, CREATED_AT, DEADLINE, 0)
    ]

    first = client.get(_paste_path(paste))
    second = client.get(_paste_path(paste))

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == text.encode("utf-8")
    assert _stored_rows(db_path) == [
        (paste["id"], text, CREATED_AT, DEADLINE, 0)
    ]


@pytest.mark.parametrize(
    ("body", "expected_status", "expected_code"),
    [
        (b"", 400, "empty_body"),
        (b"x" * (MAX_BYTES + 1), 413, "too_large"),
        (b"\xff\xfehello", 400, "invalid_utf8"),
    ],
    ids=["empty-body", "oversized-body", "invalid-utf8-body"],
)
def test_a_flagged_create_rejected_for_its_body_stores_nothing(
    client: TestClient,
    db_path: Path,
    fake_clock: FakeClock,
    body: bytes,
    expected_status: int,
    expected_code: str,
) -> None:
    """BURN-003: opt-in does not change the create rejections or let them store."""
    fake_clock.instant = CREATED_AT
    response = client.post(PASTES_PATH + BURN_AFTER_READ_TRUE_QUERY, content=body)

    assert response.status_code == expected_status
    assert response.json() == {"error": expected_code}
    assert _stored_rows(db_path) == []


def test_a_non_boolean_burn_after_read_flag_is_rejected_and_stores_nothing(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """BURN-004: a malformed flag is a 422, never silently true or false."""
    fake_clock.instant = CREATED_AT
    response = client.post(
        PASTES_PATH + BURN_AFTER_READ_INVALID_QUERY, content=b"valid text"
    )

    assert response.status_code == 422
    assert _stored_rows(db_path) == []


def test_concurrent_consume_of_a_burn_after_read_paste_returns_it_exactly_once(
    db_path: Path,
) -> None:
    """BURN-005: the read path's delete is atomic, so only one reader wins."""
    text = "only one racing reader receives this\n"
    paste_id = "R" * 22

    opened = Store(db_path)
    try:
        opened.insert(paste_id, text, CREATED_AT, DEADLINE, True)

        results: list[Paste | None] = []

        def read_once() -> None:
            results.append(opened.consume(paste_id))

        readers = [threading.Thread(target=read_once) for _ in range(12)]
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join()

        served = [paste for paste in results if paste is not None]

        assert len(results) == 12
        assert len(served) == 1
        assert served[0].text == text
        assert served[0].burn_after_read is True
        assert results.count(None) == 11
        assert opened.get(paste_id) is None
        assert opened.count() == 0
        assert opened.text_bytes() == 0
    finally:
        opened.close()


def test_an_unread_burn_after_read_paste_is_collected_by_delete_expired_at_its_deadline(
    db_path: Path,
) -> None:
    """BURN-006: housekeeping collects an unread flagged paste, at its deadline."""
    paste_id = "D" * 22
    text = "never read before the sweep\n"

    opened = Store(db_path)
    try:
        opened.insert(paste_id, text, CREATED_AT, DEADLINE, True)

        # One instant before the deadline the flagged paste is still stored and
        # still flagged: burn-after-read must not shorten its lifetime.
        assert opened.delete_expired(DEADLINE - 1) == 0
        before = opened.get(paste_id)
        assert before is not None
        assert before.text == text
        assert before.burn_after_read is True

        # From the deadline on the sweeper's one statement collects it, exactly
        # as it collects an ordinary expired row.
        assert opened.delete_expired(DEADLINE) == 1
        assert opened.get(paste_id) is None
        assert opened.count() == 0
        assert opened.text_bytes() == 0
    finally:
        opened.close()


def test_migration_keeps_an_old_rows_stored_deadline_and_adds_the_flag_as_false(
    db_path: Path,
) -> None:
    """BURN-007: the migration is in-place, so a stored deadline does not move."""
    old_id = "M" * 22
    old_text = "stored before the flag column existed\n"
    created_at = CREATED_AT + 0.5
    expires_at = created_at + config.PASTE_TTL_SECONDS

    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE pastes (
              id         TEXT PRIMARY KEY,
              text       TEXT NOT NULL,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL
            );
            CREATE INDEX pastes_expires_at ON pastes (expires_at);
            """
        )
        connection.execute(
            "INSERT INTO pastes (id, text, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (old_id, old_text, created_at, expires_at),
        )
        connection.commit()
    finally:
        connection.close()

    opened = Store(db_path)
    try:
        stored = opened.get(old_id)

        assert stored is not None
        assert stored.text == old_text
        assert stored.created_at == created_at
        assert stored.expires_at == expires_at
        assert stored.burn_after_read is False
    finally:
        opened.close()
