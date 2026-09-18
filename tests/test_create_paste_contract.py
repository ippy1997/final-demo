"""Task 6 acceptance cases for the create route (TASKS.md item 6).

TASKS.md item 6 puts the create journey in one line: read the raw body with the
1 MiB cap, 413 before storing when it is over, 400 with nothing stored when it
is empty or not valid UTF-8, insert ``created_at``/``expires_at``, and answer
201 JSON ``{id, url, expires_at}`` with an absolute url built from the request
(PRD.md items 1 and 7). These cases pin each clause of that line, and the
values the later tasks build on:

- tc001: the response is 201 JSON with exactly the three documented fields, an
  id in the documented 22-character URL-safe shape, an absolute url ending in
  that id, and the deadline as an ISO-8601 UTC instant three hours after the
  creation instant (PRD.md's applied default: "Create returns JSON containing
  the link (``url``), the id, and the expiry instant").
- tc002: the url is absolute and built from the request's own host, so the
  string works pasted into a browser as-is.
- tc003: the ceiling's boundary — a body of exactly 1 MiB is accepted and
  stored whole, one byte more is 413.
- tc004: an empty body is 400 ``empty_body`` with the store untouched.
- tc005: a body that is not valid UTF-8 is 400 ``invalid_utf8`` with the store
  untouched, over several shapes of invalid bytes.
- tc006: an oversized body is 413 ``too_large``, and item 7's measurement holds
  with pastes already in the table: the row count and the stored bytes do not
  move.
- tc007: ``created_at`` and ``expires_at`` are inserted from the injected
  clock, exactly three hours apart, and the deadline the response reports is
  the deadline that was stored (PRD.md item 4).
- tc008: the text is inserted verbatim — newlines, outer whitespace,
  non-ASCII, emoji and a string that reads like SQL (PRD.md item 2).
- tc009: a body over the ceiling sent as a stream with no ``Content-Length``
  is caught while reading, not only from the header (ARCHITECTURE.md Create).
- tc010: a ``Content-Length`` over the ceiling answers 413 on its own.
- tc011: the route takes its instant from ``clock.now`` and never reads the
  real clock (PRD.md Success).
- tc012: two submissions of the same text are two rows under two ids — no
  deduplication and no id reuse (PRD.md item 9; complements the cases TASKS.md
  item 10 will add for the pair of deadlines).

``tests/test_create_paste.py`` (same task) covers the same route from the
caller's side: the response fields, the verbatim round-trip of a realistic
body, the byte-versus-character reading of the ceiling, the parameterised
invalid UTF-8 shapes, the rejections leaving stored pastes alone and the
injected clock.

What needs the read route is recorded as belonging to TASKS.md items 7, 8 and
11: opening the returned link and seeing the text, and the uniform 404 once the
paste is past its deadline. Nothing here reaches the store through a second
API: the cases read the throwaway database the app was started on, which is
where item 7's "nothing is stored" and item 6's "insert ``created_at``/
``expires_at``" are observable.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import clock, config

# One paste creation, at an instant the case chooses (PRD.md item 4).
CREATED_AT = 1_700_000_000.0

# The documented response shape (ARCHITECTURE.md Create, PRD.md's applied
# defaults) and the documented id format (PRD.md item 3).
DOCUMENTED_RESPONSE_FIELDS = {"id", "url", "expires_at"}
DOCUMENTED_ID_LENGTH_CHARS = 22
ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{22}\Z")

# The error contract's codes for the create route (ARCHITECTURE.md Error
# contract).
EMPTY_BODY_CODE = "empty_body"
INVALID_UTF8_CODE = "invalid_utf8"
TOO_LARGE_CODE = "too_large"

# The ceiling, read from config so the boundary cases cannot drift from it.
MAX_BYTES = config.MAX_PASTE_BYTES

# The path the returned link points at (ARCHITECTURE.md Routes).
PASTES_PATH = "/pastes"

# Shapes of body that are not valid UTF-8: bytes that never are, an invalid
# two-byte sequence, a truncated three-byte sequence and a surrogate, which
# UTF-8 does not encode.
NOT_UTF8_BODIES = [
    b"\xff\xfehello",
    b"\xc3\x28",
    b"\xe2\x98",
    b"\xed\xa0\x80",
]


class _ClockReadForbidden(AssertionError):
    """Raised when the real clock is read while a fake one is installed."""


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant."""

    def time(self) -> float:
        raise _ClockReadForbidden("the real clock was read while a fake one was set")


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``."""
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _stored_text_bytes(db_path: Path) -> int:
    """The UTF-8 size of everything stored, the unit item 7's ceiling uses."""
    return sum(len(text.encode("utf-8")) for _, text, _, _ in _stored_rows(db_path))


def _over_the_ceiling_stream() -> Iterator[bytes]:
    """A body past the ceiling, sent as a stream with no length declared."""

    def chunks() -> Iterator[bytes]:
        for _ in range(11):
            yield b"x" * 100_000

    return chunks()


def test_tc001_the_create_response_carries_the_id_the_url_and_the_deadline(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """201 with ``{id, url, expires_at}``, the deadline three hours out (item 6)."""
    fake_clock.instant = CREATED_AT

    response = client.post("/pastes", content=b"a paste")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == DOCUMENTED_RESPONSE_FIELDS

    identifier = body["id"]
    assert len(identifier) == DOCUMENTED_ID_LENGTH_CHARS
    assert ID_PATTERN.match(identifier) is not None

    assert body["url"].startswith("http://")
    assert body["url"].endswith(PASTES_PATH + "/" + identifier)

    deadline = datetime.fromisoformat(body["expires_at"])
    assert deadline.tzinfo is not None
    assert deadline.utcoffset() == timedelta(0)
    assert deadline == datetime.fromtimestamp(
        CREATED_AT + config.PASTE_TTL_SECONDS, timezone.utc
    )


def test_tc002_the_url_is_absolute_and_built_from_the_requests_host(
    client: TestClient,
) -> None:
    """PRD.md's applied default: the returned string is complete as returned."""
    response = client.post(
        PASTES_PATH,
        content=b"text",
        headers={"host": "pastes.example.test:8443"},
    )

    body = response.json()
    assert body["url"] == (
        "http://pastes.example.test:8443" + PASTES_PATH + "/" + body["id"]
    )


def test_tc003_the_ceiling_boundary_is_one_mebibyte(
    client: TestClient, db_path: Path
) -> None:
    """Exactly the ceiling is stored whole; one byte more is 413 (PRD.md item 7)."""
    at_the_ceiling = "l" * MAX_BYTES

    accepted = client.post(PASTES_PATH, content=at_the_ceiling.encode("utf-8"))
    rejected = client.post(PASTES_PATH, content=b"l" * (MAX_BYTES + 1))

    assert accepted.status_code == 201
    assert rejected.status_code == 413
    assert rejected.json() == {"error": TOO_LARGE_CODE}

    assert [row[1] for row in _stored_rows(db_path)] == [at_the_ceiling]
    assert _stored_text_bytes(db_path) == MAX_BYTES


def test_tc004_an_empty_body_is_400_empty_body_with_nothing_stored(
    client: TestClient, db_path: Path
) -> None:
    """PRD.md item 7: a POST with an empty body is rejected and stores nothing."""
    response = client.post(PASTES_PATH, content=b"")

    assert response.status_code == 400
    assert response.json() == {"error": EMPTY_BODY_CODE}
    assert _stored_rows(db_path) == []
    assert _stored_text_bytes(db_path) == 0


@pytest.mark.parametrize("body", NOT_UTF8_BODIES)
def test_tc005_a_body_that_is_not_valid_utf8_is_400_invalid_utf8(
    client: TestClient, db_path: Path, body: bytes
) -> None:
    """PRD.md item 7: a body that is not valid UTF-8 is rejected, nothing stored."""
    response = client.post(PASTES_PATH, content=body)

    assert response.status_code == 400
    assert response.json() == {"error": INVALID_UTF8_CODE}
    assert _stored_rows(db_path) == []


def test_tc006_an_oversized_body_leaves_the_stored_pastes_and_bytes_unchanged(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 7's measurement, with pastes already stored before the 413."""
    fake_clock.instant = CREATED_AT
    for index in range(3):
        assert client.post(PASTES_PATH, content=f"paste {index}".encode()).status_code == 201

    rows_before = _stored_rows(db_path)
    bytes_before = _stored_text_bytes(db_path)

    response = client.post(PASTES_PATH, content=b"x" * (MAX_BYTES + 1))

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == rows_before
    assert _stored_text_bytes(db_path) == bytes_before
    assert len(rows_before) == 3


def test_tc007_created_at_and_expires_at_are_inserted_from_the_injected_clock(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Item 6's insert and PRD.md item 4's instants, as the row holds them."""
    fake_clock.instant = CREATED_AT

    body = client.post(PASTES_PATH, content=b"text").json()

    (row,) = _stored_rows(db_path)
    identifier, text, created_at, expires_at = row

    assert identifier == body["id"]
    assert text == "text"
    assert created_at == CREATED_AT
    assert expires_at == CREATED_AT + config.PASTE_TTL_SECONDS
    assert expires_at - created_at == 3 * 60 * 60
    assert datetime.fromisoformat(body["expires_at"]) == datetime.fromtimestamp(
        expires_at, timezone.utc
    )


def test_tc008_the_text_is_inserted_verbatim(client: TestClient, db_path: Path) -> None:
    """PRD.md item 2: newlines, outer whitespace, non-ASCII and emoji unchanged."""
    text = "  first line\nsecond line\r\n\tthird ☃ é 😀  "

    response = client.post(PASTES_PATH, content=text.encode("utf-8"))

    assert response.status_code == 201
    (row,) = _stored_rows(db_path)
    assert row == (
        response.json()["id"],
        text,
        row[2],
        row[3],
    )

    hostile = "'); DROP TABLE pastes;--"
    assert client.post(PASTES_PATH, content=hostile.encode()).status_code == 201
    assert {row[1] for row in _stored_rows(db_path)} == {text, hostile}


def test_tc009_a_streamed_body_with_no_length_is_capped_while_reading(
    client: TestClient, db_path: Path
) -> None:
    """ARCHITECTURE.md Create: the cap applies to the stream, not only the header."""
    response = client.post(PASTES_PATH, content=_over_the_ceiling_stream())

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == []


def test_tc010_a_declared_length_over_the_ceiling_is_413_on_its_own(
    client: TestClient, db_path: Path
) -> None:
    """ARCHITECTURE.md Create: an over-large Content-Length never reaches the store."""
    response = client.post(
        PASTES_PATH,
        content=b"short",
        headers={"content-length": str(MAX_BYTES + 1)},
    )

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == []


def test_tc011_the_route_never_reads_the_real_clock(
    client: TestClient, db_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD.md Success: the instant is injected, so expiry needs no waiting."""
    fake_clock.instant = CREATED_AT
    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    response = client.post(PASTES_PATH, content=b"text")

    assert response.status_code == 201
    (row,) = _stored_rows(db_path)
    assert row[2:] == (CREATED_AT, CREATED_AT + config.PASTE_TTL_SECONDS)


def test_tc012_the_same_text_twice_is_two_rows_under_two_ids(
    client: TestClient, db_path: Path
) -> None:
    """PRD.md item 9: no deduplication, and the route never reuses an id."""
    first = client.post(PASTES_PATH, content=b"same text").json()
    second = client.post(PASTES_PATH, content=b"same text").json()

    assert first["id"] != second["id"]
    assert first["url"] != second["url"]

    stored = {row[0]: row[1] for row in _stored_rows(db_path)}
    assert stored == {first["id"]: "same text", second["id"]: "same text"}
    assert _stored_text_bytes(db_path) == 2 * len("same text".encode("utf-8"))
