"""Task 11: the end-to-end journey, POST to link to text to the identical 404
(TASKS.md item 11).

TASKS.md item 11 is one line: POST through the test client, open the returned
url and see the text byte-for-byte, advance the clock, then check that the
expired id, a made-up id and a malformed id return the same status, body and
headers. Its success criterion is the one journey PRD.md's First version
promises — "One end-to-end test walks that path from POST to displayed text to
the identical 404" — and the requirements it walks are items 1 to 5: the
creator POSTs text and gets a link (item 1), the reader opens the link and sees
the text exactly as it was pasted (item 2), a wrong but well-formed id never
reaches someone else's paste (item 3), the deadline is three hours from
creation and is crossed on an injected clock rather than by waiting (item 4),
and from the deadline on the link is indistinguishable from one that never
existed (item 5).

The other modules already cover their own tasks from the caller's side —
``tests/test_create_paste.py`` the create response, ``tests/test_read_paste.py``
the read and its miss cases, ``tests/test_expiry_boundary.py`` the boundary
pair, ``tests/test_reclaim.py`` the sweeper — but each of them stops at its own
task's edge, and none of them walks the whole path in one case. That is what
this module adds, and it is deliberately the shortest thing that proves the
journey: every case creates its paste through ``POST /pastes`` and reads it
back by opening the absolute url the create response returned, exactly as it
was returned, rather than by rebuilding a path from the id.

- tc001: the journey in one case. POST the text, open the returned link and see
  the text byte-for-byte, advance the clock to the deadline, then compare the
  expired link, a made-up id and a malformed id: same status, same body, same
  headers, with nothing of the paste in the expired answer (PRD.md items 1-5).
- tc002: the byte-for-byte clause over the shapes PRD.md item 2 names — newlines,
  leading and trailing whitespace, non-ASCII characters and emoji — each one
  posted and then opened through the returned link.
- tc003: the expired, made-up and malformed answers as one comparison, over
  several shapes of malformed id, including ones that do not match the read
  route at all, with no ``Retry-After`` or other header that could hint that the
  paste existed (PRD.md item 5, ARCHITECTURE.md Error contract).
- tc004: the expired read reclaims the row and the stored text, while the two
  ids that were never issued store nothing, measured in the throwaway database
  the app was started on (PRD.md item 6, ARCHITECTURE.md Read).
- tc005: the journey crosses the deadline on the injected clock and not on the
  machine's, so no case here waits three hours (PRD.md Success).
- tc006: a made-up id and a malformed id are already the uniform 404 while the
  paste is alive, and the paste's own link still resolves — a wrong id never
  reaches the stored text, whichever side of the deadline it is asked on
  (PRD.md item 3).
- tc007: the instant the create response reported as ``expires_at`` is the
  instant the returned link obeys: served one second before it, the uniform 404
  from it on (PRD.md item 4; that the reported deadline is the enforced one is
  not decided by any other case, which compare against the constant instead).
- tc008: the same link opened the way PRD.md's paste reader opens it — with a
  browser's headers — still comes back as ``text/plain`` with the exact bytes
  and nothing that would make a browser download it (PRD.md item 2 and Users).
- tc009: that browser-style request past the deadline is the very same 404 an
  unknown id gets, so the reader with no tooling beyond a browser learns
  nothing either (PRD.md item 5 and Users).
- tc010: the largest paste the service accepts — a body of exactly
  ``config.MAX_PASTE_BYTES`` — round-trips byte-for-byte through its returned
  link and dies at its own deadline like any other (PRD.md item 2 and item 7's
  ceiling, walked through the journey).

The clock is the fixture's ``FakeClock``, assigned between two requests
(``tests/conftest.py``), so the expiry is observed without sleeping and without
reading the machine's real clock (PRD.md item 4 and Success). What belongs to
other tasks stays out of this file: the unmatched path and the single registered
handler are TASKS.md item 7, the sweeper is item 9, and the restart journey is
item 12.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import clock, config

# The instant the paste is created at here and the deadline the create route
# derives from it (PRD.md item 4): the three hours between the two are the
# lifetime the journey crosses, so the second value is derived rather than
# written out.
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# The ids that were never issued: one in the documented 22-character URL-safe
# shape, which is the "wrong but well-formed id" of PRD.md item 3, and ids that
# are not in that shape at all, which PRD.md item 5 requires to answer exactly
# the same. Two of the malformed shapes carry a path separator or a space, so
# they do not even reach the read route: the answer must not depend on that.
UNKNOWN_ID = "Z" * 22
MALFORMED_IDS = [
    "no-such-id",
    "A" * 21,
    "A" * 23,
    "not/a/single/segment",
    "with space",
]

# The headers that would tell a reader "this paste expired" rather than "this id
# never existed" if the 404 carried one (PRD.md item 5).
HINT_HEADERS = ("retry-after", "www-authenticate", "location")

# What a browser sends when a person follows the link, which is how PRD.md's
# paste reader arrives: an HTML-preferring navigation with no API client behind
# it. The service has to answer with plain text anyway (PRD.md item 2 and
# Users), and with the same 404 once the paste is gone (PRD.md item 5).
BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-GB,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "cross-site",
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
}

# The text the whole journey is walked with: PRD.md item 2's shapes — newlines,
# leading and trailing whitespace, non-ASCII characters and emoji — so an answer
# that trimmed, re-encoded or rendered the paste could not come back byte for
# byte, and realistic enough to be the stack trace PRD.md's Goal describes.
JOURNEY_TEXT = (
    "  a stack trace a teammate will open  \n"
    "Traceback (most recent call last):\n"
    '\tFile "app.py", line 42, in <module>\n'
    "    run()\n"
    "ValueError: é ☃ 😀 — the text must survive byte for byte\r\n"
    "  trailing whitespace kept  \n"
)

# The shapes the byte-for-byte clause is walked over, each one posted and then
# opened through the returned link (PRD.md item 2).
VERBATIM_BODIES = [
    JOURNEY_TEXT,
    "one line with no trailing newline",
    "  \t leading and trailing whitespace \t  \n",
    "non-ASCII: é — snowman ☃ Ünïcödé × ᚠᚢᚦ\n",
    "emoji 😀🎉👍🏽🇬🇧\n",
    "\ufeffa byte order mark\x00and a NUL byte",
]

# The largest body the service accepts, bracketed by a first and a last line so
# a paste that came back truncated or padded anywhere would not compare equal
# (PRD.md item 2 at PRD.md item 7's ceiling).
CEILING_HEAD = "first line of the largest allowed paste\n"
CEILING_TAIL = "\nlast line of the largest allowed paste\n"


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant.

    Installed in place of ``clock.time`` to prove the journey's creation instant
    and its deadline answer come from the injected clock and never from the
    machine's, which is what lets a case cross three hours without waiting
    (PRD.md item 4 and Success).
    """

    def time(self) -> float:
        raise AssertionError("the real clock was read while a fake one was set")


def _ceiling_text() -> str:
    """A text whose UTF-8 encoding is exactly ``config.MAX_PASTE_BYTES`` bytes.

    Every character here is ASCII, so the byte length is the character count and
    the head and the tail sit at the two ends of the ceiling (PRD.md item 7's
    boundary, which PRD.md item 2's round trip has to hold at too).
    """
    filler = (
        config.MAX_PASTE_BYTES
        - len(CEILING_HEAD.encode("utf-8"))
        - len(CEILING_TAIL.encode("utf-8"))
    )
    return CEILING_HEAD + "x" * filler + CEILING_TAIL


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so "the row the journey created is the row that was served" and "the
    expired read reclaimed it" are measured on the database rather than on an
    answer from the service (PRD.md items 1, 4 and 6).
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
    client: TestClient, fake_clock: FakeClock, text: str = JOURNEY_TEXT
) -> dict[str, str]:
    """Post ``text`` at the creation instant; return the create response body.

    Creating through the route rather than by hand keeps every case on the
    journey TASKS.md item 11 names: the link opened below is the one the create
    response returned, and the deadline crossed is the one the create route
    stored (ARCHITECTURE.md Create).
    """
    fake_clock.instant = CREATED_AT
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def test_tc001_the_journey_from_post_to_link_to_text_to_the_identical_404(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 11's success criterion, walked in one case (PRD.md items 1-5)."""
    paste = _created_paste(client, fake_clock)

    # The create response carries the link and the deadline, and the row holds
    # the text and both instants the route reported (PRD.md items 1 and 4).
    assert paste["url"].startswith("http://")
    assert paste["url"].endswith(f"{PASTES_PATH}/{paste['id']}")
    assert _stored_rows(db_path) == [(paste["id"], JOURNEY_TEXT, CREATED_AT, DEADLINE)]

    # The reader opens the returned url exactly as it was returned: an absolute
    # link to this service's own host, not a path rebuilt from the id.
    served = client.get(paste["url"])

    assert served.status_code == 200
    assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert served.text == JOURNEY_TEXT
    assert served.content == JOURNEY_TEXT.encode("utf-8")

    # Three hours after creation the same link is gone, and so is every other id
    # that never resolved: one status, one body, the same headers (PRD.md item
    # 5). The clock is moved rather than waited on (PRD.md item 4 and Success).
    fake_clock.instant = DEADLINE
    responses = {
        "expired paste": client.get(paste["url"]),
        "made-up id": client.get(f"{PASTES_PATH}/{UNKNOWN_ID}"),
        "malformed id": client.get(f"{PASTES_PATH}/{MALFORMED_IDS[0]}"),
    }

    reference = responses["made-up id"]
    assert reference.status_code == 404
    assert reference.content == NOT_FOUND_BODY
    assert reference.json() == {"error": NOT_FOUND_CODE}

    for case, response in responses.items():
        assert response.status_code == reference.status_code, case
        assert response.content == reference.content, case
        assert dict(response.headers) == dict(reference.headers), case

    # Nothing of the paste is in the answer: not its text, not its id, not a
    # countdown to a deadline that no longer has meaning (PRD.md item 5).
    expired = responses["expired paste"]
    assert JOURNEY_TEXT.encode("utf-8") not in expired.content
    assert paste["id"] not in expired.text
    for header in HINT_HEADERS:
        assert header not in expired.headers, header


@pytest.mark.parametrize("text", VERBATIM_BODIES)
def test_tc002_the_returned_link_shows_the_posted_text_byte_for_byte(
    client: TestClient, fake_clock: FakeClock, text: str
) -> None:
    """PRD.md item 2: the response body equals the submitted body exactly."""
    paste = _created_paste(client, fake_clock, text)

    served = client.get(paste["url"])

    assert served.status_code == 200
    assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert served.content == text.encode("utf-8")
    # Plain text a browser displays, not a document offered as a download.
    assert "content-disposition" not in served.headers


@pytest.mark.parametrize("malformed_id", MALFORMED_IDS)
def test_tc003_the_expired_made_up_and_malformed_answers_are_identical(
    client: TestClient, fake_clock: FakeClock, malformed_id: str
) -> None:
    """PRD.md item 5: same status, body and headers, and no expiry hint."""
    paste = _created_paste(client, fake_clock)

    fake_clock.instant = DEADLINE
    responses = {
        "expired paste": client.get(paste["url"]),
        "made-up id": client.get(f"{PASTES_PATH}/{UNKNOWN_ID}"),
        "malformed id": client.get(f"{PASTES_PATH}/{malformed_id}"),
    }

    reference = responses["made-up id"]
    assert reference.status_code == 404
    assert reference.content == NOT_FOUND_BODY
    assert reference.json() == {"error": NOT_FOUND_CODE}

    for case, response in responses.items():
        assert response.status_code == reference.status_code, case
        assert response.content == reference.content, case
        assert dict(response.headers) == dict(reference.headers), case

    assert "retry-after" not in reference.headers
    assert "location" not in reference.headers


def test_tc004_the_expired_read_reclaims_the_text_and_the_misses_store_nothing(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Items 5 and 6: the expired row is deleted first, and a miss changes nothing."""
    paste = _created_paste(client, fake_clock)
    assert _stored_rows(db_path) != []
    assert _stored_text_bytes(db_path) == len(JOURNEY_TEXT.encode("utf-8"))

    fake_clock.instant = DEADLINE
    misses = [
        client.get(paste["url"]),
        client.get(f"{PASTES_PATH}/{UNKNOWN_ID}"),
        client.get(f"{PASTES_PATH}/{MALFORMED_IDS[0]}"),
    ]

    assert [response.status_code for response in misses] == [404, 404, 404]
    assert _stored_rows(db_path) == []
    assert _stored_text_bytes(db_path) == 0


def test_tc005_the_journey_crosses_the_deadline_on_the_injected_clock(
    client: TestClient, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD.md Success: expiry is observed by moving the clock, never by waiting."""
    paste = _created_paste(client, fake_clock)

    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    alive = client.get(paste["url"])
    assert alive.status_code == 200
    assert alive.content == JOURNEY_TEXT.encode("utf-8")

    fake_clock.instant = DEADLINE
    expired = client.get(paste["url"])
    assert expired.status_code == 404
    assert expired.content == NOT_FOUND_BODY


def test_tc006_a_wrong_id_is_already_the_uniform_404_while_the_paste_lives(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 3: a wrong id never reaches someone else's paste."""
    paste = _created_paste(client, fake_clock)

    misses = [client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")]
    misses += [client.get(f"{PASTES_PATH}/{paste_id}") for paste_id in MALFORMED_IDS]

    reference = misses[0]
    assert reference.status_code == 404
    assert reference.content == NOT_FOUND_BODY

    for response in misses:
        assert response.status_code == reference.status_code
        assert response.content == reference.content
        assert dict(response.headers) == dict(reference.headers)
        assert JOURNEY_TEXT.encode("utf-8") not in response.content

    # The paste that does exist is untouched by the misses and still resolves on
    # the link the create response returned.
    still_there = client.get(paste["url"])
    assert still_there.status_code == 200
    assert still_there.content == JOURNEY_TEXT.encode("utf-8")


def test_tc007_the_reported_deadline_is_the_instant_the_link_stops_working(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: the instant the creator is told is the instant enforced."""
    paste = _created_paste(client, fake_clock)

    # The deadline travels back as an absolute UTC instant, and it is the
    # creation instant plus the fixed three hours (PRD.md item 4).
    reported = datetime.fromisoformat(paste["expires_at"])
    assert reported.utcoffset() == timedelta(0)
    assert reported.timestamp() == DEADLINE

    # One second before the instant the response named, the link still serves
    # the text; at that instant it is gone, with no grace period (the strict
    # boundary PRD.md's defaults fix).
    fake_clock.instant = reported.timestamp() - 1
    alive = client.get(paste["url"])
    assert alive.status_code == 200
    assert alive.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert alive.content == JOURNEY_TEXT.encode("utf-8")

    fake_clock.instant = reported.timestamp()
    expired = client.get(paste["url"])
    unknown = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert expired.status_code == 404
    assert expired.content == NOT_FOUND_BODY
    assert expired.json() == {"error": NOT_FOUND_CODE}
    assert expired.content == unknown.content
    assert dict(expired.headers) == dict(unknown.headers)


def test_tc008_a_browser_reader_sees_the_text_as_plain_text_byte_for_byte(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 2 and Users: a browser displays the text, it does not download it."""
    paste = _created_paste(client, fake_clock)

    served = client.get(paste["url"], headers=BROWSER_HEADERS)

    assert served.status_code == 200
    assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert "content-disposition" not in served.headers
    assert served.content == JOURNEY_TEXT.encode("utf-8")


def test_tc009_a_browser_reader_past_the_deadline_gets_the_same_404(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 5 and Users: the reader is told nothing about a dead paste."""
    paste = _created_paste(client, fake_clock)

    fake_clock.instant = DEADLINE
    browser = client.get(paste["url"], headers=BROWSER_HEADERS)
    unknown = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert browser.status_code == 404
    assert browser.content == NOT_FOUND_BODY
    assert browser.json() == {"error": NOT_FOUND_CODE}
    assert browser.content == unknown.content
    assert dict(browser.headers) == dict(unknown.headers)
    assert JOURNEY_TEXT.encode("utf-8") not in browser.content
    for header in HINT_HEADERS:
        assert header not in browser.headers, header


def test_tc010_a_paste_at_the_size_ceiling_round_trips_and_dies_at_its_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 2 at its largest input: exactly 1 MiB, byte for byte, then 404."""
    text = _ceiling_text()
    assert len(text.encode("utf-8")) == config.MAX_PASTE_BYTES

    paste = _created_paste(client, fake_clock, text)

    served = client.get(paste["url"])

    assert served.status_code == 200
    assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert len(served.content) == config.MAX_PASTE_BYTES
    assert served.content == text.encode("utf-8")
    assert _stored_text_bytes(db_path) == config.MAX_PASTE_BYTES

    fake_clock.instant = DEADLINE
    expired = client.get(paste["url"])
    unknown = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert expired.status_code == 404
    assert expired.content == NOT_FOUND_BODY
    assert expired.content == unknown.content
    assert dict(expired.headers) == dict(unknown.headers)
    assert _stored_rows(db_path) == []
    assert _stored_text_bytes(db_path) == 0
