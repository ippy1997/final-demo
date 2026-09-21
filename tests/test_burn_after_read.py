"""Burn-after-read pastes: opt-in deletion on the first successful GET.

A pastebin's burn-after-read feature is deliberately not a default. The
first GET may come from a link previewer or crawler, not the human the
creator sent the link to, so only a create request that asks for it —
``POST /pastes?burn_after_read=true`` — stores a paste whose first successful
read removes it. This file pins that contract from the caller's side:

- the query flag is opt-in: an ordinary create stores ``burn_after_read`` as
  false and reads do not consume it, while a flagged create stores true;
- a flagged paste is served exactly once, byte for byte, and the same link is
  then the app's one 404 — the same status, body and headers as an id that was
  never issued;
- a flagged paste that is never read still expires at its stored deadline and
  is reclaimed rather than served;
- the flag is stored with the row and survives reopening the database, so a
  restart does not lose it;
- an existing database created before the column existed is migrated in place,
  and its old pastes come back as ordinary, never-burn-after-read pastes.

The route-level cases create through ``POST /pastes`` and read through the
link the create response returned, exactly as the first version's journey
does. The store-level cases use a throwaway file and the real ``Store``, the
same way ``tests/test_store.py`` and ``tests/test_store_contract.py`` do.
The clock is the fixture's ``FakeClock`` throughout, so expiry is observed by
moving the clock rather than waiting (PRD.md Success).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config
from app.store import Store

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

# The create query that opts a paste in. Everything else about the request is
# the raw-text create the first version already documents.
BURN_AFTER_READ_QUERY = "?burn_after_read=true"


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


def _columns(db_path: Path) -> set[str]:
    """The column names of the ``pastes`` table, read outside the store."""
    connection = sqlite3.connect(db_path)
    try:
        return {
            row[1]
            for row in connection.execute("PRAGMA table_info(pastes)").fetchall()
        }
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
    burn_after_read: bool = False,
) -> dict[str, str]:
    """Post ``text`` at the case's creation instant and return the create body."""
    fake_clock.instant = CREATED_AT
    path = PASTES_PATH + (BURN_AFTER_READ_QUERY if burn_after_read else "")
    response = client.post(path, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def test_the_flag_is_opt_in_and_an_ordinary_paste_is_read_without_being_consumed(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """A plain create keeps the old behaviour: many reads, no deletion."""
    text = "an ordinary paste\nread many times\n"
    paste = _created_paste(client, fake_clock, text)

    assert _stored_rows(db_path) == [
        (paste["id"], text, CREATED_AT, DEADLINE, 0)
    ]

    first = client.get(_paste_path(paste))
    second = client.get(_paste_path(paste))

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == text.encode("utf-8")
    assert _stored_rows(db_path) == [(paste["id"], text, CREATED_AT, DEADLINE, 0)]


def test_a_flagged_create_stores_the_burn_after_read_flag(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """The opt-in query is what makes a paste consumable, and it is stored."""
    text = "burn me on first read\n"
    paste = _created_paste(client, fake_clock, text, burn_after_read=True)

    # The create response keeps the documented three fields: the flag is a
    # storage property, not a new response field.
    assert set(paste) == {"id", "url", "expires_at"}
    assert _stored_rows(db_path) == [
        (paste["id"], text, CREATED_AT, DEADLINE, 1)
    ]


def test_a_burn_after_read_paste_is_served_once_and_then_is_the_uniform_404(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """The first successful GET returns the text and deletes the row."""
    text = "  first and only read\n\ttabbed 😀\n"
    paste = _created_paste(client, fake_clock, text, burn_after_read=True)

    first = client.get(_paste_path(paste))

    assert first.status_code == 200
    assert first.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert first.content == text.encode("utf-8")
    assert _stored_rows(db_path) == []

    # The same link is now indistinguishable from an id that was never issued
    # (PRD.md item 5). The row is already gone, so the read route sees a miss.
    second = client.get(_paste_path(paste))
    unknown = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert second.status_code == 404
    assert second.content == NOT_FOUND_BODY
    assert second.json() == {"error": NOT_FOUND_CODE}
    assert second.content == unknown.content
    assert dict(second.headers) == dict(unknown.headers)
    assert text.encode("utf-8") not in second.content
    assert paste["id"] not in second.text


def test_a_burn_after_read_paste_still_expires_at_its_stored_deadline_if_unread(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Burn-after-read shortens a read paste, not its lifetime.

    An unread flagged paste is still in the table until its deadline and is
    then reclaimed, never served; the first GET after the deadline is the same
    404 as an expired ordinary paste and an id that was never issued.
    """
    text = "never read before it expires\n"
    paste = _created_paste(client, fake_clock, text, burn_after_read=True)
    assert _stored_rows(db_path) == [
        (paste["id"], text, CREATED_AT, DEADLINE, 1)
    ]

    fake_clock.instant = DEADLINE
    response = client.get(_paste_path(paste))
    unknown = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert response.status_code == 404
    assert response.content == NOT_FOUND_BODY
    assert response.json() == {"error": NOT_FOUND_CODE}
    assert response.content == unknown.content
    assert dict(response.headers) == dict(unknown.headers)
    assert text.encode("utf-8") not in response.content
    assert _stored_rows(db_path) == []


def test_the_burn_after_read_flag_survives_reopening_the_database(
    db_path: Path,
) -> None:
    """The flag is a stored column, so a restart does not lose it."""
    text = "burn after the restart\n"
    paste_id = "B" * 22

    first = Store(db_path)
    try:
        first.insert(paste_id, text, CREATED_AT, DEADLINE, True)
    finally:
        first.close()

    reopened = Store(db_path)
    try:
        stored = reopened.get(paste_id)

        assert stored is not None
        assert stored.burn_after_read is True
        assert stored.text == text

        consumed = reopened.consume(paste_id)

        assert consumed is not None
        assert consumed.text == text
        assert reopened.get(paste_id) is None
        assert reopened.count() == 0
    finally:
        reopened.close()


def test_a_database_from_before_the_column_existed_is_migrated_in_place(
    db_path: Path,
) -> None:
    """An old file gets the flag column with its existing rows kept and unmarked."""
    old_id = "C" * 22
    old_text = "stored before the flag column existed\n"

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
            (old_id, old_text, CREATED_AT, DEADLINE),
        )
        connection.commit()
    finally:
        connection.close()

    opened = Store(db_path)
    try:
        assert "burn_after_read" in _columns(db_path)

        stored = opened.get(old_id)

        assert stored is not None
        assert stored.text == old_text
        assert stored.burn_after_read is False

        # The migrated old paste behaves like an ordinary paste: consume
        # returns it without deleting it.
        assert opened.consume(old_id) is not None
        assert opened.get(old_id) is not None
        assert opened.count() == 1
        assert opened.text_bytes() == len(old_text.encode("utf-8"))
    finally:
        opened.close()
