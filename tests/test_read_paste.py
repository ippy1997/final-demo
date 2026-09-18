"""Task 7: the read route, ``GET /pastes/{paste_id}`` (TASKS.md item 7).

TASKS.md item 7 is one line of behaviour: a GET on a paste's link answers 200
``text/plain; charset=utf-8`` with the stored text unchanged, and one app-wide
404 handler answers with the identical body and headers for a missing id, an
expired paste whose row is deleted first, and an unmatched path (PRD.md items
2 and 5).

The cases below walk the journey from the caller's side — the text is posted
through ``POST /pastes`` and read back through the link the create response
returned, compared byte for byte over the shapes PRD.md item 2 names — and then
pin every way the read can fail: an id that was never issued, a malformed id
and a paste whose deadline has passed. The expired case also reads the
throwaway database the app was started on, because "the row is deleted first"
is what makes PRD.md item 6's reclamation true and not only PRD.md item 5's
uniform 404, and this task has to deliver both.

The boundary the route itself enforces is checked on both sides of it: one
second before the deadline the text comes back, at the deadline it does not.
The fuller boundary work — a paste read repeatedly before its deadline still
dying at it, reclamation by the sweeper and the restart journey — is TASKS.md
items 8, 9 and 12. ``tests/test_read_paste_contract.py`` (same task) states the
same requirements as acceptance cases, including the three-way identity of the
404s and the single registered handler behind them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import clock, config

# The instant a paste is created at here, and the deadline the create route
# derives from it (PRD.md item 4).
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The path the create response's url points at, and the two things every
# answer of this task is made of (ARCHITECTURE.md Routes, Error contract).
PASTES_PATH = "/pastes"
NOT_FOUND_CODE = "not_found"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"

# The ways a read can miss: an id in the documented shape that was never
# issued, ids that are not in that shape at all, and a path no route matches
# (PRD.md items 3 and 5).
UNKNOWN_ID = "Z" * 22
MALFORMED_IDS = ["no-such-id", "A" * 21, "A" * 23, "with space", "sl/ash"]
UNMATCHED_PATH = "/nothing-here"

# The bodies the round trip is checked over: the shapes PRD.md item 2 names —
# newlines, leading and trailing whitespace, non-ASCII characters and emoji —
# plus a body that is only whitespace and one that reads like SQL.
VERBATIM_BODIES = [
    "single line",
    "two\nlines\n",
    "  leading and trailing spaces  ",
    "\t\ttabs\ttabbed\n\r\nand a CRLF\r\n",
    "é — snowman ☃ Ünïcödé × ᚠᚢᚦ",
    "emoji 😀🎉👍🏽🇬🇧",
    "\ufeffa byte order mark\x00and a NUL byte",
    "  \t\n\r  ",
    "'); DROP TABLE pastes;--",
    "line without a trailing newline",
]


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant.

    Installed in place of ``clock.time`` to prove the read path decides an
    expiry from the injected dependency and never from the machine's real
    clock (PRD.md Success).
    """

    def time(self) -> float:
        raise AssertionError("the real clock was read while a fake one was set")


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, which is what shows that an expired paste's row is really deleted rather
    than filtered out of the answer (PRD.md item 6).
    """
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


def _created_paste(
    client: TestClient, fake_clock: FakeClock, text: str
) -> dict[str, str]:
    """Post ``text`` at the case's creation instant and return the response body.

    Creating through the route rather than by hand keeps the case on the
    journey TASKS.md item 7 names: the reader opens the link the create
    response returned, and the deadline the read enforces is the one the create
    route stored (ARCHITECTURE.md Create and Read).
    """
    fake_clock.instant = CREATED_AT
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned.

    The url is absolute and built from the request's own host, so the case
    reads the path out of it rather than rebuilding one from the id: that is
    the path a browser would open (PRD.md applied defaults).
    """
    return urlsplit(paste["url"]).path


def test_the_link_the_create_response_returned_serves_the_stored_text(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 1 completed: opening the returned link shows that text."""
    text = "a paste a teammate will open\n"
    paste = _created_paste(client, fake_clock, text)

    assert _paste_path(paste) == f"{PASTES_PATH}/{paste['id']}"

    response = client.get(_paste_path(paste))

    assert response.status_code == 200
    assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert response.text == text
    assert response.content == text.encode("utf-8")


@pytest.mark.parametrize("text", VERBATIM_BODIES)
def test_the_stored_text_comes_back_byte_for_byte(
    client: TestClient, fake_clock: FakeClock, text: str
) -> None:
    """PRD.md item 2: the response body equals the submitted body exactly."""
    paste = _created_paste(client, fake_clock, text)

    response = client.get(_paste_path(paste))

    assert response.status_code == 200
    assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert response.content == text.encode("utf-8")
    # Plain text, served rather than offered as a file: a browser displays it.
    assert "content-disposition" not in response.headers


def test_a_paste_one_second_before_its_deadline_is_still_served(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """The route serves while the deadline is in the future (PRD.md item 4)."""
    paste = _created_paste(client, fake_clock, "still here")
    fake_clock.instant = DEADLINE - 1

    response = client.get(_paste_path(paste))

    assert response.status_code == 200
    assert response.content == b"still here"


def test_reading_a_paste_neither_changes_its_text_nor_its_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """A read is a read: the stored row is the same before and after (item 4)."""
    text = "read me twice"
    paste = _created_paste(client, fake_clock, text)
    rows_before = _stored_rows(db_path)

    first = client.get(_paste_path(paste))
    second = client.get(_paste_path(paste))

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == text.encode("utf-8")
    assert _stored_rows(db_path) == rows_before == [
        (paste["id"], text, CREATED_AT, DEADLINE)
    ]
    assert _stored_text_bytes(db_path) == len(text.encode("utf-8"))


def test_an_id_that_was_never_issued_is_404_not_found(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 3: a wrong but well-formed id is a 404 and someone else's text."""
    stored = _created_paste(client, fake_clock, "someone else's text")

    response = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"error": NOT_FOUND_CODE}
    assert "someone else's text" not in response.text
    # The paste that does exist is untouched by the miss.
    assert _stored_rows(db_path) == [
        (stored["id"], "someone else's text", CREATED_AT, DEADLINE)
    ]


@pytest.mark.parametrize("paste_id", MALFORMED_IDS)
def test_a_malformed_id_is_404_not_found(
    client: TestClient, fake_clock: FakeClock, paste_id: str
) -> None:
    """A nonsense id is the same nothing as a well-formed miss (PRD.md item 5)."""
    _created_paste(client, fake_clock, "the stored text")

    response = client.get(f"{PASTES_PATH}/{paste_id}")

    assert response.status_code == 404
    assert response.json() == {"error": NOT_FOUND_CODE}
    assert "the stored text" not in response.text


def test_an_unmatched_path_is_404_not_found(client: TestClient) -> None:
    """A path no route matches gets the app's 404, not the framework's."""
    response = client.get(UNMATCHED_PATH)

    assert response.status_code == 404
    assert response.json() == {"error": NOT_FOUND_CODE}
    assert response.text == '{"error":"not_found"}'


def test_an_expired_paste_is_404_and_its_row_is_deleted_before_the_answer(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Items 5 and 6: the uniform 404, with the expired row already gone."""
    text = "expired and must not be served"
    paste = _created_paste(client, fake_clock, text)
    assert _stored_rows(db_path) != []

    fake_clock.instant = DEADLINE
    response = client.get(_paste_path(paste))

    assert response.status_code == 404
    assert response.json() == {"error": NOT_FOUND_CODE}
    assert _stored_rows(db_path) == []
    assert _stored_text_bytes(db_path) == 0
    assert text not in response.text
    assert paste["id"] not in response.text

    # Already deleted: asking again is the same 404, and nothing else moved.
    again = client.get(_paste_path(paste))
    assert again.status_code == 404
    assert again.content == response.content
    assert _stored_rows(db_path) == []


def test_the_expired_404_is_indistinguishable_from_an_unknown_one(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 5: status, body and headers are identical across the cases."""
    expired = _created_paste(client, fake_clock, "gone for good")
    fake_clock.instant = DEADLINE

    responses = {
        "expired paste": client.get(_paste_path(expired)),
        "unknown id": client.get(f"{PASTES_PATH}/{UNKNOWN_ID}"),
        "malformed id": client.get(f"{PASTES_PATH}/{MALFORMED_IDS[0]}"),
        "unmatched path": client.get(UNMATCHED_PATH),
    }

    reference = responses["unknown id"]
    assert reference.status_code == 404
    assert reference.content == b'{"error":"not_found"}'

    for case, response in responses.items():
        assert response.status_code == reference.status_code, case
        assert response.content == reference.content, case
        assert dict(response.headers) == dict(reference.headers), case

    # No header that separates "this paste expired" from "this id never
    # existed" (PRD.md item 5).
    assert "retry-after" not in reference.headers
    assert "www-authenticate" not in reference.headers
    assert "location" not in reference.headers


def test_the_read_route_takes_the_instant_from_the_injected_clock(
    client: TestClient, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD.md Success: no real clock on the read path, so expiry needs no waiting."""
    text = "the text"
    paste = _created_paste(client, fake_clock, text)

    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    live = client.get(_paste_path(paste))
    assert live.status_code == 200
    assert live.content == text.encode("utf-8")

    fake_clock.instant = DEADLINE
    expired = client.get(_paste_path(paste))
    assert expired.status_code == 404
    assert expired.json() == {"error": NOT_FOUND_CODE}
