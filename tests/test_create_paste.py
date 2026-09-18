"""Task 6: the create route, ``POST /pastes`` (TASKS.md item 6).

The route reads the raw request body with the 1 MiB ceiling applied, answers
413 before storing anything when the body is over it, answers 400 with nothing
stored when the body is empty or not valid UTF-8, inserts ``created_at`` and
``expires_at``, and answers 201 with ``{id, url, expires_at}`` where the url is
absolute and built from the request itself (TASKS.md item 6, PRD.md items 1
and 7).

These cases pin the journey a caller takes — a POST carrying text returns a
success status whose body contains a link that carries the new id — and the
two rejections with their measurement from item 7: after a 400 or a 413 the
number of stored pastes and the stored bytes are unchanged, checked against
the throwaway database the app was started on rather than against the
response. The clock is the fixture's fake one, so the creation instant and the
deadline the route stores are decided here rather than by the machine's real
clock (PRD.md item 4, ARCHITECTURE.md Parts: table row "Clock").

What the reader side of the journey needs — opening the returned link and
seeing the text — belongs to TASKS.md items 7, 8 and 11, which is why this
file reads the stored rows back directly instead.
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

# One paste creation, at an instant the test chooses (PRD.md item 4: the
# deadline measures wall-clock time from creation).
CREATED_AT = 1_700_000_000.0

# The id format ARCHITECTURE.md's decision row fixes: 22 characters of
# unpadded URL-safe base64 over 128 random bits (PRD.md item 3).
ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{22}\Z")

# The error codes ARCHITECTURE.md's error contract names for a rejected create.
EMPTY_BODY_CODE = "empty_body"
INVALID_UTF8_CODE = "invalid_utf8"
TOO_LARGE_CODE = "too_large"

# The ceiling the tests work at: the one constant PRD.md's defaults fix.
MAX_BYTES = config.MAX_PASTE_BYTES


class _ClockReadForbidden(AssertionError):
    """Raised when the real clock is read while a fake one is installed."""


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant."""

    def time(self) -> float:
        raise _ClockReadForbidden("the real clock was read while a fake one was set")


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, which is what makes item 7's "nothing is stored" measurable: the
    route's own response cannot answer that.
    """
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


def _oversized_stream(
    chunk: bytes = b"x" * 100_000, chunk_count: int = 11
) -> Iterator[bytes]:
    """A body over the ceiling, sent as a stream with no length declared.

    The test client sends an iterable body without a ``Content-Length``, so
    this is the case the route's stream read has to catch rather than the
    header check (ARCHITECTURE.md Create).
    """

    def chunks() -> Iterator[bytes]:
        for _ in range(chunk_count):
            yield chunk

    return chunks()


def test_posting_text_answers_201_with_the_id_the_url_and_the_deadline(
    client: TestClient,
) -> None:
    """PRD.md item 1: a POST carrying text returns a success status with a link."""
    response = client.post("/pastes", content=b"a stack trace")

    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"

    body = response.json()
    assert set(body) == {"id", "url", "expires_at"}
    assert ID_PATTERN.match(body["id"]) is not None
    assert body["url"].endswith("/pastes/" + body["id"])
    assert body["url"].startswith("http://")


def test_the_returned_url_is_built_from_the_requests_own_host(
    client: TestClient,
) -> None:
    """The link can be pasted into a browser as-is (PRD.md applied defaults)."""
    response = client.post(
        "/pastes", content=b"text", headers={"host": "pastes.example.test:8443"}
    )

    assert response.status_code == 201
    assert response.json()["url"] == (
        "http://pastes.example.test:8443/pastes/" + response.json()["id"]
    )


def test_the_paste_is_stored_under_the_returned_id_with_the_injected_instant(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """The insert carries the id, the text and both instants (ARCHITECTURE.md Create)."""
    fake_clock.instant = CREATED_AT

    body = client.post("/pastes", content=b"stored text").json()

    assert _stored_rows(db_path) == [
        (body["id"], "stored text", CREATED_AT, CREATED_AT + config.PASTE_TTL_SECONDS)
    ]


def test_the_deadline_in_the_response_is_creation_plus_three_hours(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: the response reports the instant the link stops working."""
    fake_clock.instant = CREATED_AT

    reported = client.post("/pastes", content=b"text").json()["expires_at"]
    deadline = datetime.fromisoformat(reported)

    assert deadline == datetime.fromtimestamp(
        CREATED_AT + config.PASTE_TTL_SECONDS, timezone.utc
    )
    assert deadline.utcoffset() == timedelta(0)


def test_the_text_is_stored_verbatim_whatever_the_request_content_type(
    client: TestClient, db_path: Path
) -> None:
    """The body is read raw and stored with no trimming or normalisation (item 2)."""
    text = "  leading\nline\r\n\ttabbed\n  é — snowman ☃ 😀  trailing  \n"

    response = client.post(
        "/pastes",
        content=text.encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 201
    (row,) = _stored_rows(db_path)
    assert row[1] == text


def test_a_body_of_exactly_the_ceiling_is_accepted_and_stored_whole(
    client: TestClient, db_path: Path
) -> None:
    """PRD.md item 7's boundary: 1 MiB is a paste, not a rejection."""
    text = "l" * MAX_BYTES

    response = client.post("/pastes", content=text.encode("utf-8"))

    assert response.status_code == 201
    (row,) = _stored_rows(db_path)
    assert row[1] == text
    assert _stored_text_bytes(db_path) == MAX_BYTES


def test_a_body_one_byte_over_the_ceiling_is_rejected_with_413(
    client: TestClient, db_path: Path
) -> None:
    """One byte past the ceiling is too large, and nothing of it is stored."""
    response = client.post("/pastes", content=b"l" * (MAX_BYTES + 1))

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == []


def test_the_ceiling_is_counted_in_bytes_not_characters(
    client: TestClient, db_path: Path
) -> None:
    """Item 7 measures the body in bytes, so multi-byte text counts as sent."""
    at_the_ceiling = "😀" * (MAX_BYTES // 4)
    over_the_ceiling = at_the_ceiling + "😀"

    assert len(at_the_ceiling.encode("utf-8")) == MAX_BYTES
    assert len(over_the_ceiling.encode("utf-8")) > MAX_BYTES

    accepted = client.post("/pastes", content=at_the_ceiling.encode("utf-8"))
    rejected = client.post("/pastes", content=over_the_ceiling.encode("utf-8"))

    assert accepted.status_code == 201
    assert rejected.status_code == 413
    assert [row[1] for row in _stored_rows(db_path)] == [at_the_ceiling]


def test_a_body_streamed_past_the_ceiling_with_no_length_is_rejected_with_413(
    client: TestClient, db_path: Path
) -> None:
    """The cap applies while reading, not only to a declared Content-Length."""
    response = client.post("/pastes", content=_oversized_stream())

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == []


def test_a_declared_length_over_the_ceiling_is_rejected_without_storing(
    client: TestClient, db_path: Path
) -> None:
    """ARCHITECTURE.md Create: a Content-Length over 1 MiB answers 413 on its own."""
    response = client.post(
        "/pastes",
        content=b"small but over-declared",
        headers={"content-length": str(MAX_BYTES + 1)},
    )

    assert response.status_code == 413
    assert response.json() == {"error": TOO_LARGE_CODE}
    assert _stored_rows(db_path) == []


def test_an_empty_body_is_rejected_with_400_and_stores_nothing(
    client: TestClient, db_path: Path
) -> None:
    """PRD.md item 7: an empty POST is a 400, and the row count does not move."""
    response = client.post("/pastes", content=b"")

    assert response.status_code == 400
    assert response.json() == {"error": EMPTY_BODY_CODE}
    assert _stored_rows(db_path) == []


def test_a_post_with_no_body_at_all_is_rejected_with_400(
    client: TestClient, db_path: Path
) -> None:
    """The same empty body, written the way a caller with no data sends it."""
    response = client.post("/pastes")

    assert response.status_code == 400
    assert response.json() == {"error": EMPTY_BODY_CODE}
    assert _stored_rows(db_path) == []


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfehello",  # bytes that are never valid UTF-8
        b"\xc3\x28",  # an invalid two-byte sequence
        b"\xe2\x98",  # a truncated three-byte sequence
        b"\xed\xa0\x80",  # a UTF-16 surrogate, which UTF-8 does not encode
    ],
)
def test_a_body_that_is_not_valid_utf8_is_rejected_with_400_and_stores_nothing(
    client: TestClient, db_path: Path, body: bytes
) -> None:
    """PRD.md item 7 and ARCHITECTURE.md's contract: 400 ``invalid_utf8``."""
    response = client.post("/pastes", content=body)

    assert response.status_code == 400
    assert response.json() == {"error": INVALID_UTF8_CODE}
    assert _stored_rows(db_path) == []


def test_a_rejected_request_leaves_the_pastes_already_stored_alone(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Item 7's measurement, with a paste in the table before the rejections."""
    fake_clock.instant = CREATED_AT
    kept = client.post("/pastes", content=b"keep me").json()

    rejected = [
        client.post("/pastes", content=b""),
        client.post("/pastes", content=b"\xff\xfe"),
        client.post("/pastes", content=b"l" * (MAX_BYTES + 1)),
    ]

    assert [response.status_code for response in rejected] == [400, 400, 413]
    assert _stored_rows(db_path) == [
        (kept["id"], "keep me", CREATED_AT, CREATED_AT + config.PASTE_TTL_SECONDS)
    ]


def test_a_rejected_request_answers_an_error_and_no_paste(client: TestClient) -> None:
    """A failure carries the code and nothing that looks like a created paste."""
    response = client.post("/pastes", content=b"")

    assert response.json() == {"error": EMPTY_BODY_CODE}
    assert "location" not in response.headers


def test_the_same_text_posted_twice_is_two_pastes_with_two_ids(
    client: TestClient, db_path: Path
) -> None:
    """No deduplication and no id reuse: two submissions are two rows (item 9)."""
    first = client.post("/pastes", content=b"identical").json()
    second = client.post("/pastes", content=b"identical").json()

    assert first["id"] != second["id"]
    stored = {row[0]: row[1] for row in _stored_rows(db_path)}
    assert stored == {first["id"]: "identical", second["id"]: "identical"}


def test_the_create_route_reads_the_injected_clock_and_never_the_real_one(
    client: TestClient, db_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instant comes from ``clock.now``, so no test waits or races (item 4)."""
    fake_clock.instant = CREATED_AT
    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    response = client.post("/pastes", content=b"text")

    assert response.status_code == 201
    (row,) = _stored_rows(db_path)
    assert row[2] == CREATED_AT
