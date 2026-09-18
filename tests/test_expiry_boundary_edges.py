"""Task 8's expiry boundary at the instants nearest the deadline it does not
already pin (TASKS.md item 8).

``tests/test_expiry_boundary.py`` states the task's three clauses — the text at
T+3h-1s, the uniform 404 at T+3h, and repeated pre-deadline reads that still
die at T+3h — against the deadline the create route reported. This module adds
the instants immediately around that boundary, each named after the acceptance
case it proves:

- tc201: at the last float below the stored deadline the text comes back
  byte-for-byte and the row is untouched — the boundary of PRD.md item 4's
  "retrievable strictly before its deadline", measured at the shortest step a
  float can take rather than at a whole second.
- tc202: at the first float past the stored deadline the answer is the uniform
  404 an unissued id gets and the row is deleted before the answer is written
  (PRD.md item 5, PRD.md item 6).
- tc203: every instant after the deadline keeps giving that same 404 from an
  empty table, so the death at T+3h is permanent rather than a single response
  (TASKS.md item 8).
- tc204: moving the clock back to before the deadline does not bring the text
  back: the expired row was deleted rather than filtered, which is what PRD.md
  item 6 and ARCHITECTURE.md's decision row ("Delete expired rows on read and
  in a sweeper", alternative "filter expiry at read time only") require.
- tc205: the instant the create response reported is the instant the boundary
  uses, probed a microsecond either side of it on a fractional creation
  instant — PRD.md item 4's "the create response reports the deadline as an
  instant", and the caller has no other way to know where the boundary is.

Every case creates its paste through ``POST /pastes`` and reads it back through
the link that response returned, so the deadline probed is the one the create
route stored and reported rather than a number this module assumes. The clock
is the fixture's ``FakeClock``, assigned between two requests: no case sleeps
three hours and none reads the machine's real clock (PRD.md Success).
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config

# The instant the cases create at and the deadline the create route derives
# from it (PRD.md item 4). The case that needs a fractional creation instant
# adds its own quarter of a second below.
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# An id in the documented shape that was never issued, which the expired answer
# must be indistinguishable from (PRD.md item 5).
UNKNOWN_ID = "Z" * 22

# The headers that would tell a reader "this paste expired" rather than "this
# id never existed" if the deadline answer carried one (PRD.md item 5).
HINT_HEADERS = ("retry-after", "www-authenticate", "location")

# The text the boundary is probed over, carrying the shapes PRD.md item 2
# names so a truncated or re-encoded answer would be visible.
PASTE_TEXT = "  a paste on the edge of its three hours — é ☃ 😀\n"

# The one microsecond step either side of a reported deadline that tc205
# probes at: short enough that a boundary rounded to the second, or a deadline
# reported without its microseconds, would land on the wrong side of it.
MICROSECOND = 1e-6


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so the instant the boundary is probed at and the deletion the deadline
    performs are measured on the database rather than on the answer (PRD.md
    items 4 and 6).
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _stored_deadline(db_path: Path) -> float:
    """The deadline of the one stored paste, as the database holds it."""
    rows = _stored_rows(db_path)
    assert len(rows) == 1, rows
    return rows[0][3]


def _reported_deadline(paste: dict[str, str]) -> float:
    """The deadline the create response reported, as Unix seconds.

    PRD.md item 4 has the create response report the deadline as an instant,
    and that reported value is the only one a caller can probe the boundary
    with, so the cases below cross the boundary at it rather than at a number
    this module recomputes (ARCHITECTURE.md Create).
    """
    return datetime.fromisoformat(paste["expires_at"]).timestamp()


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned."""
    return urlsplit(paste["url"]).path


def _created_paste(
    client: TestClient,
    fake_clock: FakeClock,
    text: str,
    instant: float = CREATED_AT,
) -> dict[str, str]:
    """Post ``text`` at ``instant`` and return the create response's body."""
    fake_clock.instant = instant
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def test_tc201_the_text_comes_back_at_the_last_instant_before_the_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: strictly before the deadline the paste is retrievable.

    The instant probed is the largest float below the stored deadline — one
    step of the float that holds it — so a boundary that rounded, or that
    required a second of margin, would answer 404 here.
    """
    paste = _created_paste(client, fake_clock, PASTE_TEXT)
    rows_at_creation = _stored_rows(db_path)
    assert rows_at_creation == [(paste["id"], PASTE_TEXT, CREATED_AT, DEADLINE)]
    deadline = _stored_deadline(db_path)

    fake_clock.instant = math.nextafter(deadline, -math.inf)
    response = client.get(_paste_path(paste))

    assert response.status_code == 200
    assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert response.content == PASTE_TEXT.encode("utf-8")
    # Serving the text is not an expiry: neither stored instant moved, so the
    # read at the edge of the lifetime wrote nothing (PRD.md item 4).
    assert _stored_rows(db_path) == rows_at_creation


def test_tc202_the_uniform_404_covers_the_first_instant_past_the_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Items 5 and 6: from the deadline on, an unissued id's 404 and a deleted row.

    The instant probed is the smallest float above the stored deadline, one
    step of the float that holds it, so the answer turning into the 404 exactly
    at the deadline is measured rather than inferred from a second later.
    """
    paste = _created_paste(client, fake_clock, PASTE_TEXT)
    deadline = _stored_deadline(db_path)

    fake_clock.instant = math.nextafter(deadline, math.inf)
    expired = client.get(_paste_path(paste))
    unknown = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert unknown.status_code == 404
    assert unknown.json() == {"error": NOT_FOUND_CODE}
    assert expired.status_code == 404
    assert expired.content == unknown.content == NOT_FOUND_BODY
    assert dict(expired.headers) == dict(unknown.headers)

    # No hint that a paste was ever here, and nothing of the text: the answer
    # is exactly the body the error contract fixes (PRD.md item 5).
    assert PASTE_TEXT.encode("utf-8") not in expired.content
    assert paste["id"] not in expired.text
    for header in HINT_HEADERS:
        assert header not in expired.headers, header

    # Deleted before the answer was written, so the text is reclaimed rather
    # than filtered out of the body (PRD.md item 6).
    assert _stored_rows(db_path) == []


def test_tc203_the_paste_stays_the_uniform_404_at_every_later_instant(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 8: the death at T+3h is permanent, not one response.

    The same link is read at the deadline and at instants spread past it; every
    answer is byte-identical to the first, so nothing about the passage of time
    after the deadline reaches the caller (PRD.md item 5).
    """
    paste = _created_paste(client, fake_clock, PASTE_TEXT)
    deadline = _stored_deadline(db_path)

    fake_clock.instant = deadline
    at_deadline = client.get(_paste_path(paste))
    assert at_deadline.status_code == 404
    assert at_deadline.content == NOT_FOUND_BODY

    later_instants = [deadline + 1, deadline + 60, deadline + config.PASTE_TTL_SECONDS]
    for instant in later_instants:
        fake_clock.instant = instant
        response = client.get(_paste_path(paste))
        assert response.status_code == 404, instant
        assert response.content == at_deadline.content, instant
        assert dict(response.headers) == dict(at_deadline.headers), instant
        assert PASTE_TEXT.encode("utf-8") not in response.content, instant

    assert _stored_rows(db_path) == []


def test_tc204_a_paste_that_died_is_not_served_when_the_clock_returns_to_before_its_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 6: the expired row is deleted, so the text is gone, not hidden.

    A read at the deadline removes the row rather than filtering it out of the
    answer (ARCHITECTURE.md Read). Once it is gone, no later instant — not even
    one before the deadline it was created with — finds the paste again: the
    three hours ended for it, and the link is as dead as an id never issued
    (PRD.md items 4, 5 and 6).
    """
    paste = _created_paste(client, fake_clock, PASTE_TEXT)
    deadline = _stored_deadline(db_path)

    fake_clock.instant = deadline
    died = client.get(_paste_path(paste))
    assert died.status_code == 404
    assert _stored_rows(db_path) == []

    fake_clock.instant = deadline - 1
    again = client.get(_paste_path(paste))

    assert again.status_code == 404
    assert again.content == NOT_FOUND_BODY
    assert dict(again.headers) == dict(died.headers)
    assert PASTE_TEXT.encode("utf-8") not in again.content
    assert _stored_rows(db_path) == []


def test_tc205_the_reported_deadline_is_the_instant_the_boundary_uses(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: the instant the caller was told is the one enforced.

    The creation instant carries a quarter of a second, so the reported
    deadline is not a whole second and a deadline rendered without its
    microseconds — or a boundary rounded to the second — would answer on the
    wrong side of a microsecond step. The probe uses only the reported value: a
    caller knows the boundary from the create response and nothing else.
    """
    creation_instant = CREATED_AT + 0.25
    paste = _created_paste(client, fake_clock, PASTE_TEXT, instant=creation_instant)

    reported = _reported_deadline(paste)
    assert reported == creation_instant + config.PASTE_TTL_SECONDS
    assert _stored_deadline(db_path) == reported

    fake_clock.instant = reported - MICROSECOND
    before = client.get(_paste_path(paste))

    assert before.status_code == 200
    assert before.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert before.content == PASTE_TEXT.encode("utf-8")

    fake_clock.instant = reported
    at = client.get(_paste_path(paste))

    assert at.status_code == 404
    assert at.content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == []
