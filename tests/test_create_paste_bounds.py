"""Task 6, the create route's boundaries and orderings (TASKS.md item 6).

TASKS.md item 6 is one line of behaviour: read the raw body with the 1 MiB
cap, answer 413 before storing when it is over, answer 400 with nothing stored
when it is empty or not valid UTF-8, insert ``created_at``/``expires_at``, and
answer 201 JSON ``{id, url, expires_at}`` with an absolute url built from the
request. ``tests/test_create_paste.py`` and ``tests/test_create_paste_contract.py``
already pin the shape of the response, the verbatim round-trip, the two
rejections and the injected clock. This file pins the edges of each of those
clauses, which is where the size rule and the order of the checks are decided:

- tc601: a body that reaches the ceiling through the stream with no
  ``Content-Length`` at all is a paste, not a rejection — the limit is "over
  1 MiB", so exactly 1 MiB has to get in (ARCHITECTURE.md Create).
- tc602: one byte past it, delivered the same way, is 413 with the store
  untouched, measured against the database the app was started on (PRD.md
  item 7: "after either the number of stored pastes is unchanged").
- tc603: a body that is both over the ceiling and not valid UTF-8 is
  413 ``too_large``: the ceiling is applied while reading and the encoding is
  checked afterwards, which is the order ARCHITECTURE.md's Create journey
  states.
- tc604: a body of exactly the ceiling whose last byte is invalid is 400
  ``invalid_utf8``, not 413 — the other side of the same order.
- tc605: the smallest non-empty body is a paste (the empty one, one byte
  earlier, is the 400 the other files cover).
- tc606: only an empty body is "empty": a body of only whitespace is a paste
  and is stored without trimming (PRD.md item 2).
- tc607: the cap is enforced on the bytes read, so a ``Content-Length`` that
  understates the body does not let it through (ARCHITECTURE.md Create: 413 if
  "the stream passes 1 MiB while reading").
- tc608: the creation instant goes into the row unrounded and the deadline is
  exactly the TTL later, reported in the response as the instant that was
  stored (PRD.md item 4).
- tc609: a body that is valid UTF-8 but unusual — a byte-order mark and a NUL
  byte — is stored unchanged: "text is stored verbatim" has no exceptions for
  control characters (PRD.md's applied defaults).
- tc610: the url is absolute with the scheme the caller reached the service
  on, not a hard-coded one, so the returned string is complete as returned
  (PRD.md's applied default: "``url`` absolute and built from the request's own
  host").

What needs the read route stays out of this task: opening the returned link
and seeing the text is TASKS.md item 7, and the end-to-end walk is item 11.
Both files for this task therefore read the stored rows back through a second
connection to the throwaway database instead of through a second API.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config
from app.main import app

# The route under test and the three codes its rejections use
# (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TOO_LARGE_CODE = "too_large"
INVALID_UTF8_CODE = "invalid_utf8"

# The ceiling, read from config rather than written as a literal, so these
# cases always sit exactly on whichever limit config sets (PRD.md item 7).
MAX_BYTES = config.MAX_PASTE_BYTES

# One creation instant, and the fractional one tc608 uses to show the stored
# instant is not rounded to whole seconds (PRD.md item 4).
CREATED_AT = 1_700_000_000.0
FRACTIONAL_CREATED_AT = CREATED_AT + 0.25

# The chunk the streamed bodies below are built from: a size that divides the
# ceiling evenly, so a body of exactly the ceiling is several full chunks and
# no partial one.
CHUNK = b"a" * 65_536


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on: the route's own response cannot show whether a rejected request stored
    anything, which is the measurement PRD.md item 7 asks for.
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _stored_text_bytes(db_path: Path) -> int:
    """The UTF-8 size of everything stored, the unit the ceiling counts."""
    return sum(len(text.encode("utf-8")) for _, text, _, _ in _stored_rows(db_path))


def _streamed_body(total_bytes: int, tail: bytes = b"") -> Iterator[bytes]:
    """A body delivered in chunks, plus an optional final ``tail``.

    An iterable body makes the test client send no ``Content-Length``, so the
    request exercises the route's read of the raw stream rather than its look
    at the declared length (ARCHITECTURE.md Create).
    """

    def chunks() -> Iterator[bytes]:
        sent = 0
        while sent < total_bytes:
            size = min(len(CHUNK), total_bytes - sent)
            yield CHUNK[:size]
            sent += size
        if tail:
            yield tail

    return chunks()


def test_tc601_a_streamed_body_of_exactly_the_ceiling_is_a_paste(
    client: TestClient, db_path: Path
) -> None:
    """The limit is "over 1 MiB": exactly the ceiling gets in and is stored whole."""
    response = client.post(PASTES_PATH, content=_streamed_body(MAX_BYTES))

    assert response.status_code == 201

    (row,) = _stored_rows(db_path)
    assert row[0] == response.json()["id"]
    assert row[1] == "a" * MAX_BYTES
    assert _stored_text_bytes(db_path) == MAX_BYTES


def test_tc602_a_streamed_body_one_byte_over_the_ceiling_is_413_and_stores_nothing(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """The first byte past the ceiling rejects the request, with the store untouched."""
    fake_clock.instant = CREATED_AT
    kept = client.post(PASTES_PATH, content=b"already here").json()
    rows_before = _stored_rows(db_path)
    bytes_before = _stored_text_bytes(db_path)

    response = client.post(PASTES_PATH, content=_streamed_body(MAX_BYTES, tail=b"!"))

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == rows_before
    assert _stored_text_bytes(db_path) == bytes_before
    assert [row[0] for row in rows_before] == [kept["id"]]


def test_tc603_an_over_ceiling_body_that_is_not_valid_utf8_is_413(
    client: TestClient, db_path: Path
) -> None:
    """The ceiling is applied while reading, before the body is decoded."""
    response = client.post(
        PASTES_PATH, content=_streamed_body(MAX_BYTES, tail=b"\xff")
    )

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == []


def test_tc604_a_body_of_exactly_the_ceiling_that_is_not_valid_utf8_is_400(
    client: TestClient, db_path: Path
) -> None:
    """At the ceiling but undecodable: the encoding check is what rejects it."""
    body = b"a" * (MAX_BYTES - 1) + b"\xff"
    assert len(body) == MAX_BYTES

    response = client.post(PASTES_PATH, content=body)

    assert response.status_code == 400
    assert response.json() == {"error": INVALID_UTF8_CODE}
    assert _stored_rows(db_path) == []


def test_tc605_a_one_byte_body_is_a_paste(client: TestClient, db_path: Path) -> None:
    """One byte of content is not an empty body, so it is stored."""
    response = client.post(PASTES_PATH, content=b"x")

    assert response.status_code == 201
    (row,) = _stored_rows(db_path)
    assert row[0] == response.json()["id"]
    assert row[1] == "x"
    assert _stored_text_bytes(db_path) == 1


def test_tc606_a_body_of_only_whitespace_is_a_paste_stored_untrimmed(
    client: TestClient, db_path: Path
) -> None:
    """Blank is not empty, and no trimming happens on the way in."""
    text = "  \t\n\r  "

    response = client.post(PASTES_PATH, content=text.encode("utf-8"))

    assert response.status_code == 201
    (row,) = _stored_rows(db_path)
    assert row[1] == text


def test_tc607_an_over_ceiling_body_with_a_smaller_declared_length_is_413(
    client: TestClient, db_path: Path
) -> None:
    """A Content-Length that understates the body cannot get it past the cap."""
    response = client.post(
        PASTES_PATH,
        content=b"z" * (MAX_BYTES + 1),
        headers={"content-length": "5"},
    )

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == []


def test_tc608_the_deadline_is_exactly_three_hours_after_a_fractional_instant(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """The injected instant is stored unrounded and the deadline is TTL later."""
    fake_clock.instant = FRACTIONAL_CREATED_AT

    response = client.post(PASTES_PATH, content=b"text")

    assert response.status_code == 201

    (row,) = _stored_rows(db_path)
    created_at, expires_at = row[2], row[3]
    assert created_at == FRACTIONAL_CREATED_AT
    assert expires_at - created_at == config.PASTE_TTL_SECONDS
    assert expires_at == FRACTIONAL_CREATED_AT + config.PASTE_TTL_SECONDS

    reported = datetime.fromisoformat(response.json()["expires_at"])
    assert reported == datetime.fromtimestamp(expires_at, timezone.utc)
    assert reported.utcoffset().total_seconds() == 0


def test_tc609_a_body_with_a_byte_order_mark_and_a_nul_byte_is_a_paste(
    client: TestClient, db_path: Path
) -> None:
    """Unusual but valid UTF-8 is text like any other and is stored unchanged."""
    text = "\ufefffirst\x00second 😀"

    response = client.post(PASTES_PATH, content=text.encode("utf-8"))

    assert response.status_code == 201
    (row,) = _stored_rows(db_path)
    assert row[1] == text


def test_tc610_the_url_keeps_the_scheme_and_host_the_caller_used(
    client: TestClient,
) -> None:
    """Absolute means complete: the link is as useful as the request that made it.

    The ``client`` fixture is here for the fake clock and the throwaway
    database it installs; the request is made by a second client whose base URL
    is an HTTPS host, which is how a caller behind TLS reaches the service. A
    url built by hand rather than from the request would come back with the
    wrong scheme, host or port.
    """
    with TestClient(app, base_url="https://pastes.example.test:8443") as https_client:
        response = https_client.post(PASTES_PATH, content=b"text")

    body = response.json()
    assert response.status_code == 201
    assert body["url"] == "https://pastes.example.test:8443/pastes/" + body["id"]
