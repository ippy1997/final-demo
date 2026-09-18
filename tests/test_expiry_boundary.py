"""Task 8: the expiry boundary with the clock under the test's control
(TASKS.md item 8).

TASKS.md item 8 is one line: with a controlled clock the text comes back at
T+3h-1s and the uniform 404 comes back at T+3h, and a paste read repeatedly
before its deadline still dies at T+3h. PRD.md item 4 fixes a paste's deadline
at three hours after creation, measured in wall-clock time and never reset by a
read; PRD.md item 5 makes the answer from the deadline on identical to the one
a link that never existed gets; ARCHITECTURE.md's Read journey writes the
comparison as a paste found with ``clock.now() >= expires_at`` deleted and
answered with the same 404, and its "Strict boundary" decision row rejects a
grace window in either direction.

Every case below creates the paste through ``POST /pastes`` and reads it back
through the link that response returned, so the instant probed is the deadline
the create route stored and reported rather than a number this module assumes.
The clock is the fixture's ``FakeClock``, assigned between two requests: no
case sleeps three hours and none reads the machine's real clock (PRD.md
Success).

``tests/test_read_paste.py`` and ``tests/test_read_paste_edges.py`` already
touch the two sides of the boundary one second apart each; what this module
adds, besides the pair measured on one paste, is the boundary at a fractional
instant, the boundary following the creation instant, the reads repeated across
the whole lifetime that must not move the deadline, and the boundary at the
stored row rather than only in the answer. The three clauses are stated as
cases here rather than in a separate acceptance module, because the task is
itself the test work for one behaviour (the boundary) and has no other surface
to pin.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import clock, config

# The instant a paste is created at here and the deadline the create route
# derives from it (PRD.md item 4). The three hours between the two are the
# point of the task, so the second value is derived rather than written out.
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The two instants the boundary is probed at: the last second that still
# resolves and the deadline itself, from which the paste is gone (PRD.md item
# 4's boundary default).
ONE_SECOND_BEFORE = DEADLINE - 1

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# The other ways a read misses, which the expired answer must not be
# distinguishable from (PRD.md item 5): an id in the documented shape that was
# never issued, an id that is not in that shape at all, and a path no route
# matches (PRD.md item 3, ARCHITECTURE.md Error contract).
UNKNOWN_ID = "Z" * 22
MALFORMED_ID = "no-such-id"
UNMATCHED_PATH = "/nothing-here"

# The headers that would tell a reader "this paste expired" rather than "this
# id never existed" if the deadline answer carried one (PRD.md item 5).
HINT_HEADERS = ("retry-after", "www-authenticate", "location")

# The text the boundary is probed over, carrying the shapes PRD.md item 2
# names so a truncated or re-encoded answer would be visible.
PASTE_TEXT = "  the paste has three hours to live — é ☃ 😀\n"


class _ClockReadForbidden(AssertionError):
    """Raised when the real clock is read while a fake one is installed."""


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant.

    Installed in place of ``clock.time`` to prove the boundary is decided by
    the injected clock and never by the machine's time, which is what lets a
    test cross it without waiting (PRD.md item 4 and Success).
    """

    def time(self) -> float:
        raise _ClockReadForbidden("the real clock was read while a fake one was set")


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so "the deadline did not move" and "the expired row is really gone" are
    measured on the database rather than on the answer (PRD.md items 4 and 6).
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _reported_deadline(paste: dict[str, str]) -> float:
    """The deadline the create response reported, as Unix seconds.

    The boundary is probed at the instant the caller was told about, so the
    case checks the reported deadline instead of assuming the create route
    added the lifetime it was supposed to (PRD.md item 4, ARCHITECTURE.md
    Create).
    """
    return datetime.fromisoformat(paste["expires_at"]).timestamp()


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned.

    The url is absolute and built from the request's own host, so the case
    reads the path out of it rather than rebuilding one from the id: that is
    the path a browser would open (PRD.md applied defaults).
    """
    return urlsplit(paste["url"]).path


def _created_paste(
    client: TestClient, fake_clock: FakeClock, text: str
) -> dict[str, str]:
    """Post ``text`` at the case's creation instant and return the response body."""
    fake_clock.instant = CREATED_AT
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def test_the_text_comes_back_one_second_before_the_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 8's first clause, probed at the reported deadline (item 4)."""
    paste = _created_paste(client, fake_clock, PASTE_TEXT)
    rows_at_creation = _stored_rows(db_path)
    # The instant the pair below straddles is the one the caller was told.
    assert _reported_deadline(paste) == DEADLINE

    fake_clock.instant = ONE_SECOND_BEFORE
    response = client.get(_paste_path(paste))

    assert response.status_code == 200
    assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert response.content == PASTE_TEXT.encode("utf-8")
    # Serving the text is not an expiry: both stored instants are still where
    # creation put them, so the read neither moved nor removed anything
    # (PRD.md item 4).
    assert _stored_rows(db_path) == rows_at_creation
    assert rows_at_creation == [(paste["id"], PASTE_TEXT, CREATED_AT, DEADLINE)]


def test_the_uniform_404_comes_back_at_the_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 8's second clause: T+3h is the uniform 404 (item 5)."""
    paste = _created_paste(client, fake_clock, PASTE_TEXT)

    fake_clock.instant = DEADLINE
    responses = {
        "expired paste": client.get(_paste_path(paste)),
        "unknown id": client.get(f"{PASTES_PATH}/{UNKNOWN_ID}"),
        "malformed id": client.get(f"{PASTES_PATH}/{MALFORMED_ID}"),
        "unmatched path": client.get(UNMATCHED_PATH),
    }

    reference = responses["unknown id"]
    assert reference.status_code == 404
    assert reference.content == NOT_FOUND_BODY
    assert reference.json() == {"error": NOT_FOUND_CODE}

    # From the deadline on, "this paste expired" and "this id never existed"
    # cannot be told apart: the same status, the same body, the same headers
    # (PRD.md item 5).
    for case, response in responses.items():
        assert response.status_code == reference.status_code, case
        assert response.content == reference.content, case
        assert dict(response.headers) == dict(reference.headers), case

    expired = responses["expired paste"]
    assert PASTE_TEXT.encode("utf-8") not in expired.content
    assert paste["id"] not in expired.text
    for header in HINT_HEADERS:
        assert header not in expired.headers, header

    # The deadline is enforced by deleting the row, not by filtering it out of
    # the answer, so the expired text is reclaimed and the second read of the
    # same id is the same 404 from an empty table (PRD.md items 5 and 6).
    assert _stored_rows(db_path) == []
    again = client.get(_paste_path(paste))
    assert again.status_code == 404
    assert again.content == expired.content


def test_one_second_separates_the_text_from_the_uniform_404(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """The pair in one case: the same link, the same text, one second apart."""
    paste = _created_paste(client, fake_clock, PASTE_TEXT)

    fake_clock.instant = ONE_SECOND_BEFORE
    before = client.get(_paste_path(paste))

    fake_clock.instant = DEADLINE
    at = client.get(_paste_path(paste))

    assert before.status_code == 200
    assert before.content == PASTE_TEXT.encode("utf-8")
    assert at.status_code == 404
    assert at.content == NOT_FOUND_BODY


@pytest.mark.parametrize(
    "creation_instant",
    [CREATED_AT, CREATED_AT + 0.25, 1_699_999_999.5],
)
def test_the_boundary_is_three_hours_after_the_creation_instant_it_saw(
    client: TestClient, fake_clock: FakeClock, creation_instant: float
) -> None:
    """The deadline tracks T, not a fixed epoch or the first request (item 4)."""
    fake_clock.instant = creation_instant
    response = client.post(PASTES_PATH, content=b"a paste")
    assert response.status_code == 201, response.text
    paste = response.json()
    deadline = creation_instant + config.PASTE_TTL_SECONDS
    assert _reported_deadline(paste) == deadline

    fake_clock.instant = deadline - 1
    before = client.get(_paste_path(paste))

    fake_clock.instant = deadline
    at = client.get(_paste_path(paste))

    assert before.status_code == 200
    assert before.content == b"a paste"
    assert at.status_code == 404
    assert at.content == NOT_FOUND_BODY


def test_the_boundary_is_strict_within_a_fraction_of_a_second(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """An instant short of the deadline resolves; the deadline itself does not."""
    paste = _created_paste(client, fake_clock, PASTE_TEXT)

    fake_clock.instant = DEADLINE - 1e-6
    just_before = client.get(_paste_path(paste))

    fake_clock.instant = DEADLINE
    at = client.get(_paste_path(paste))

    assert just_before.status_code == 200
    assert just_before.content == PASTE_TEXT.encode("utf-8")
    assert at.status_code == 404
    assert at.content == NOT_FOUND_BODY


def test_a_paste_read_repeatedly_before_its_deadline_still_dies_at_the_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 8's third clause: reads do not extend the deadline (item 4)."""
    paste = _created_paste(client, fake_clock, PASTE_TEXT)
    rows_at_creation = _stored_rows(db_path)

    # Spread across the three hours and ending one second short of the
    # deadline, so the reads span the whole lifetime rather than clustering at
    # its start.
    read_instants = [
        CREATED_AT + offset
        for offset in range(0, config.PASTE_TTL_SECONDS, 7 * 60)
    ] + [DEADLINE - 60, ONE_SECOND_BEFORE]
    assert len(read_instants) > 20

    for instant in read_instants:
        fake_clock.instant = instant
        response = client.get(_paste_path(paste))
        assert response.status_code == 200, instant
        assert response.content == PASTE_TEXT.encode("utf-8"), instant

    # Not one of those reads wrote anything: the row creation inserted is still
    # the row, its deadline included, so nothing was extended or renewed
    # (PRD.md item 4 and its "no sliding lifetime" default).
    assert _stored_rows(db_path) == rows_at_creation

    fake_clock.instant = DEADLINE
    expired = client.get(_paste_path(paste))

    assert expired.status_code == 404
    assert expired.content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == []


def test_the_boundary_comes_from_the_injected_clock_and_never_the_real_one(
    client: TestClient, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD.md Success: expiry is crossed by moving the clock, never by waiting."""
    paste = _created_paste(client, fake_clock, PASTE_TEXT)

    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    fake_clock.instant = ONE_SECOND_BEFORE
    before = client.get(_paste_path(paste))

    fake_clock.instant = DEADLINE
    at = client.get(_paste_path(paste))

    assert before.status_code == 200
    assert before.content == PASTE_TEXT.encode("utf-8")
    assert at.status_code == 404
    assert at.content == NOT_FOUND_BODY
