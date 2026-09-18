"""Task 7 acceptance cases for the read route (TASKS.md item 7).

TASKS.md item 7 is one line: ``GET /pastes/{paste_id}`` answers 200
``text/plain; charset=utf-8`` with the stored text unchanged, and one app-wide
404 handler gives the identical body and headers for a missing id, an expired
paste (row deleted first) and an unmatched path (PRD.md items 2 and 5). These
cases pin each clause of that line and the properties the later tasks build on:

- tc001: the stored text is served unchanged, and the row is what was served:
  the response body is exactly what ``POST /pastes`` stored (PRD.md item 2).
- tc002: the answer is the bare text served as ``text/plain; charset=utf-8``
  with no wrapper, no rendering and no download disposition — a browser
  displays it rather than saving it (PRD.md item 2, ARCHITECTURE.md Read).
- tc003: the three 404s — an id never issued, a paste past its deadline, a path
  no route matches — have the same status, the same body and the same headers,
  compared against each other rather than against a literal (PRD.md item 5).
- tc004: that body is the contract's ``{"error": "not_found"}`` and the
  response carries no header that could hint at expiry, such as
  ``Retry-After`` (ARCHITECTURE.md Error contract, PRD.md item 5).
- tc005: the expired paste's row is deleted before the answer is written, so
  items 5 and 6 hold together and a second request for the same id is the same
  404 from an empty table (ARCHITECTURE.md Read).
- tc006: the expired answer carries none of the paste's text, not even the
  start of it: the body is exactly the error body (PRD.md item 5).
- tc007: both sources of a 404 reach one handler — the app registers a single
  handler for HTTP exceptions and it is the module's, which is what makes the
  different cases indistinguishable rather than coincidentally equal.
- tc008: the uniform 404 covers exactly the 404s: an error with any other
  status, including the 405 the router answers when the path exists for another
  method, keeps the handling FastAPI installs by default.
- tc009: the instant comes from ``clock.now`` and never from the real clock, on
  the 200 path and on the expired 404 (PRD.md Success).
- tc010: the route is a GET on the documented path, so the link the create
  response returns resolves (ARCHITECTURE.md Routes).
- tc011: a read writes nothing, so a read before the deadline leaves the stored
  deadline exactly where the create route put it (PRD.md item 4).
- tc012: a near-miss id never reaches another paste's text, and the paste that
  does exist keeps resolving (PRD.md item 3).

``tests/test_read_paste.py`` (same task) covers the same route from the
caller's side: the link the create response returned, the verbatim round trip
over the shapes item 2 names, the miss cases, the deleted row, and the identity
of the four 404 responses. What needs the next tasks is recorded as belonging
to them: the T+3h-1s and T+3h boundary pair, repeated reads dying at the same
deadline, the sweeper and the restart journey are TASKS.md items 8, 9 and 12.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import clock, config, main
from app.main import app

# The instant the cases create at and the deadline the create route derives
# from it (PRD.md item 4).
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The documented route, content type and error contract for a miss
# (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
PASTE_PATH_FORMAT = "/pastes/{paste_id}"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# An id in the documented shape that was never issued, ids that are not in that
# shape at all, and a path no route matches (PRD.md items 3 and 5). An empty id
# is deliberately absent: ``/pastes/`` is the create path with a trailing
# slash, so the router answers 405 before any route runs, which tc008 covers.
UNKNOWN_ID = "Z" * 22
MALFORMED_IDS = ["no-such-id", "A" * 21, "A" * 23, "not/one/id"]
UNMATCHED_PATH = "/no-such-path"

# A text long enough and distinctive enough that a truncated or partial answer
# would be visible, with the shapes PRD.md item 2 names.
LONG_TEXT = (
    "  first line with trailing spaces  \n"
    "second line — é ☃ 😀\n"
    "\tthird line\r\n"
    "and a fourth line that ends the paste\n"
) * 20

# The headers that would tell a reader "this paste expired" rather than "this
# id never existed" if the 404 carried one (PRD.md item 5).
HINT_HEADERS = ("retry-after", "www-authenticate", "location")


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant."""

    def time(self) -> float:
        raise AssertionError("the real clock was read while a fake one was set")


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
    """The UTF-8 size of everything stored, the unit item 6 counts in."""
    return sum(len(text.encode("utf-8")) for _, text, _, _ in _stored_rows(db_path))


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned."""
    return urlsplit(paste["url"]).path


def _created_paste(
    client: TestClient, fake_clock: FakeClock, text: str
) -> dict[str, str]:
    """Post ``text`` at the case's creation instant and return the response body."""
    fake_clock.instant = CREATED_AT
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def test_tc001_a_stored_paste_is_served_unchanged(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 2: the body equals the stored text, and the store holds it too."""
    text = "  verbatim\nline\r\n\ttabbed é ☃ 😀  trailing  \n"
    paste = _created_paste(client, fake_clock, text)

    response = client.get(_paste_path(paste))

    assert response.status_code == 200
    (row,) = _stored_rows(db_path)
    assert row == (paste["id"], text, CREATED_AT, DEADLINE)
    assert response.content == row[1].encode("utf-8") == text.encode("utf-8")


def test_tc002_the_text_is_served_as_plain_utf8_text_with_no_wrapper(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 2: plain text a browser displays, not a document or a download."""
    text = "plain text, not JSON, not HTML\n"
    paste = _created_paste(client, fake_clock, text)

    response = client.get(_paste_path(paste))

    assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert "content-disposition" not in response.headers
    assert response.content == text.encode("utf-8")
    assert response.headers["content-length"] == str(len(text.encode("utf-8")))

    # Nothing was wrapped around the text: the body starts and ends with it.
    assert not response.content.startswith(b"{")
    assert not response.content.startswith(b"<")


def test_tc003_the_three_404s_are_identical_in_status_body_and_headers(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 5: expired, never issued and unmatched cannot be told apart."""
    expired = _created_paste(client, fake_clock, LONG_TEXT)
    fake_clock.instant = DEADLINE

    expired_response = client.get(_paste_path(expired))
    unknown_response = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")
    unmatched_response = client.get(UNMATCHED_PATH)

    assert expired_response.status_code == unknown_response.status_code == 404
    assert unmatched_response.status_code == 404

    assert expired_response.content == unknown_response.content
    assert unmatched_response.content == unknown_response.content
    assert expired_response.content == NOT_FOUND_BODY

    assert dict(expired_response.headers) == dict(unknown_response.headers)
    assert dict(unmatched_response.headers) == dict(unknown_response.headers)


def test_tc004_the_404_body_is_the_contract_code_and_carries_no_hint(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """ARCHITECTURE.md's error contract: ``{"error": "not_found"}`` and no hint."""
    expired = _created_paste(client, fake_clock, "gone")
    fake_clock.instant = DEADLINE

    response = client.get(_paste_path(expired))

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"] == NOT_FOUND_CODE

    for header in HINT_HEADERS:
        assert header not in response.headers, header

    # No countdown and no remaining time, in a header or in the body.
    assert str(int(DEADLINE)) not in response.text


def test_tc005_the_expired_row_is_deleted_before_the_answer(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Items 5 and 6: the text is reclaimed by the read, not hidden behind a filter."""
    text = "expired text that must be reclaimed"
    paste = _created_paste(client, fake_clock, text)
    assert _stored_text_bytes(db_path) == len(text.encode("utf-8"))

    fake_clock.instant = DEADLINE
    first = client.get(_paste_path(paste))

    assert first.status_code == 404
    assert _stored_rows(db_path) == []
    assert _stored_text_bytes(db_path) == 0

    # The row is gone, so the second request is answered from an empty table
    # and is still the same 404.
    second = client.get(_paste_path(paste))
    assert second.status_code == 404
    assert second.content == first.content == NOT_FOUND_BODY
    assert dict(second.headers) == dict(first.headers)


def test_tc006_the_expired_answer_carries_none_of_the_text_not_even_a_prefix(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 5: the text is not returned, partially returned or hinted at."""
    paste = _created_paste(client, fake_clock, LONG_TEXT)
    fake_clock.instant = DEADLINE

    response = client.get(_paste_path(paste))

    assert response.status_code == 404
    assert response.content == NOT_FOUND_BODY
    for prefix_length in (1, 5, 20, 500):
        assert LONG_TEXT[:prefix_length].encode("utf-8") not in response.content
    assert str(len(LONG_TEXT)).encode("ascii") not in response.content


def test_tc007_both_404_sources_reach_the_one_registered_handler() -> None:
    """Item 7's "one app-wide 404 handler": the route's and the router's.

    The read route raises for an id it cannot serve and the router raises for a
    path no route matches; both are ``StarletteHTTPException``, and the app
    registers exactly one handler for that family. The behavioural half of the
    claim — that the two answers are byte-identical — is tc003.
    """
    handlers = {
        exception_type: handler
        for exception_type, handler in app.exception_handlers.items()
        if isinstance(exception_type, type)
        and issubclass(exception_type, StarletteHTTPException)
    }

    assert set(handlers) == {StarletteHTTPException}
    assert handlers[StarletteHTTPException] is main._http_exception_handler


def test_tc008_only_a_404_is_rewritten_and_other_errors_keep_their_handling(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """A 405 — the path exists for another method — is not the uniform 404.

    ``GET /pastes`` and ``GET /pastes/`` match the create path, so the router
    answers 405 before the read route is reached; that path is not "unmatched".
    The uniform 404 belongs to a lookup the route could not serve, not to every
    request for a path that exists in some other form.
    """
    paste = _created_paste(client, fake_clock, "text")

    wrong_method = client.put(_paste_path(paste))
    no_id = client.get(PASTES_PATH)

    for response in (wrong_method, no_id):
        assert response.status_code == 405
        assert "error" not in response.json()
        assert "detail" in response.json()

    assert wrong_method.headers["allow"] == "GET"
    assert no_id.headers["allow"] == "POST"


def test_tc009_the_read_path_never_reads_the_real_clock(
    client: TestClient, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD.md Success: the instant is injected, so the boundary needs no waiting."""
    text = "the text"
    paste = _created_paste(client, fake_clock, text)

    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    assert client.get(_paste_path(paste)).status_code == 200

    fake_clock.instant = DEADLINE
    expired = client.get(_paste_path(paste))
    assert expired.status_code == 404
    assert expired.content == NOT_FOUND_BODY


def test_tc010_the_read_route_is_the_documented_get_on_the_documented_path() -> None:
    """ARCHITECTURE.md Routes: the link the create response returns resolves."""
    paths = app.openapi()["paths"]

    assert "get" in paths[PASTE_PATH_FORMAT]
    assert "post" in paths["/pastes"]
    assert "get" in paths["/health"]


def test_tc011_a_read_writes_nothing_so_the_deadline_stays_put(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: no read moves the deadline the create route stored."""
    text = "read me"
    paste = _created_paste(client, fake_clock, text)
    rows_before = _stored_rows(db_path)

    for instant in (CREATED_AT, CREATED_AT + 1, DEADLINE - 1):
        fake_clock.instant = instant
        assert client.get(_paste_path(paste)).status_code == 200
        assert _stored_rows(db_path) == rows_before

    assert rows_before == [(paste["id"], text, CREATED_AT, DEADLINE)]


def test_tc012_a_near_miss_id_never_reaches_the_stored_text(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 3: a wrong id is a 404, and the right id keeps working."""
    text = "the stored text"
    paste = _created_paste(client, fake_clock, text)

    candidates = [
        paste["id"].lower(),
        paste["id"][:-1],
        paste["id"] + "A",
        "A" + paste["id"][1:],
        UNKNOWN_ID,
        *MALFORMED_IDS,
    ]

    checked = 0
    for candidate in candidates:
        if candidate == paste["id"]:
            # A candidate that came out identical to the drawn id is not a
            # near-miss: lower-casing or re-heading the id can coincide with it.
            continue
        response = client.get(f"{PASTES_PATH}/{candidate}")
        assert response.status_code == 404, candidate
        assert response.content == NOT_FOUND_BODY, candidate
        checked += 1

    # At most the two rewritten candidates can have coincided with the id, so
    # the measurement above is over most of the list rather than over nothing.
    assert checked >= len(candidates) - 2

    found = client.get(_paste_path(paste))
    assert found.status_code == 200
    assert found.content == text.encode("utf-8")
