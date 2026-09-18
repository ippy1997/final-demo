"""Task 10: repeated submissions are separate pastes (TASKS.md item 10).

TASKS.md item 10 is one line: the same text posted twice yields two ids, both
resolve to it, and each 404s on its own deadline. PRD.md item 9 is what that
serves — "repeated submissions are separate pastes": two different links, both
resolving to that text, dying at their own deadlines three hours after their
own creation, with no deduplication and no shared lifetime.

The design delivers that in one place without a route having to know about
repeats: an id is drawn per submission from ``ids.new_id()`` over the whole
128-bit space and never from the text (ARCHITECTURE.md's decision row "Ids from
``secrets.token_urlsafe(16)``", whose "why" names this requirement: hashing
would also break item 9), and the deadline is computed once at creation from
``clock.now()`` and stored on the row, so two rows share a text and nothing
else (ARCHITECTURE.md Data and Create). What this task adds is the tests that
state the line as one journey, because the cases the earlier tasks already
carry stop short of it: ``tests/test_create_paste.py`` and
``tests/test_create_paste_contract.py`` (tc012) pin two ids and two stored rows
from two posts, and ``tests/test_store_contract.py`` (tc012) pins two rows from
one text at the store's own API; none of those opens both links, and none
crosses the two deadlines.

The cases below walk the line from the caller's side. The same text is posted
twice at two instants, and both ids, both links, both stored rows, the text
each link serves and the two 404s are all measured — the stored rows through a
second connection to the throwaway database the app was started on, exactly as
``tests/test_read_paste.py`` measures reclamation (TASKS.md items 7 and 9). The
clock is the fixture's ``FakeClock``, assigned between two requests, so the
deadline of each submission is crossed without waiting and without reading the
machine's real clock (PRD.md item 4 and Success).

What the other tasks own stays out of this file: the sweeper reclaiming each of
the two rows on its own tick, the generator call behind each id, fractional
creation instants and what happens when the same text is posted again after
the first copy was reclaimed are TASKS.md item 10 at its edges
(``tests/test_repeated_submissions_edges.py``), and the restart journey is item
12.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config

# The instants the two submissions are posted at, and the deadline the create
# route derives from each (PRD.md item 4). The second submission lands an hour
# after the first, which is what makes the two deadlines distinguishable
# without waiting for either.
CREATED_AT = 1_700_000_000.0
ONE_HOUR_SECONDS = 60 * 60
SECOND_CREATED_AT = CREATED_AT + ONE_HOUR_SECONDS
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS
SECOND_DEADLINE = SECOND_CREATED_AT + config.PASTE_TTL_SECONDS

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# An id in the documented shape that was never issued, which every expired
# answer must be indistinguishable from (PRD.md items 3 and 5).
UNKNOWN_ID = "Z" * 22

# The text both submissions carry. It holds the shapes PRD.md item 2 names, so
# a reply that re-encoded, trimmed or otherwise merged the two pastes would be
# visible rather than coincidentally equal.
REPEATED_TEXT = "the same paste posted twice — é ☃ 😀\n\ttabbed\r\n  spaced  \n"


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so "two rows, not one" and "each row kept its own deadline" are
    measured on the database rather than on the create responses (PRD.md item
    9).
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _stored_text_bytes(rows: list[tuple[str, str, float, float]]) -> int:
    """The UTF-8 size of the text in ``rows``, the unit item 6 counts in."""
    return sum(len(text.encode("utf-8")) for _, text, _, _ in rows)


def _stored_ids(rows: list[tuple[str, str, float, float]]) -> set[str]:
    """The ids in ``rows``, for asking which of the two submissions is left."""
    return {row[0] for row in rows}


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned.

    The url is absolute and built from the request's own host, so the case
    reads the path out of it rather than rebuilding one from the id: that is
    the path a browser would open (PRD.md applied defaults).
    """
    return urlsplit(paste["url"]).path


def _reported_deadline(paste: dict[str, str]) -> float:
    """The deadline the create response reported, as Unix seconds.

    PRD.md item 4 has the create response report the deadline as an instant,
    so the cases compare the instant each submission was told about rather than
    a number they recompute (ARCHITECTURE.md Create).
    """
    return datetime.fromisoformat(paste["expires_at"]).timestamp()


def _created_paste(
    client: TestClient,
    fake_clock: FakeClock,
    text: str,
    instant: float = CREATED_AT,
) -> dict[str, str]:
    """Post ``text`` with the clock set to ``instant``; return the response body.

    Posting through the route rather than inserting by hand keeps every case on
    the journey TASKS.md item 10 names: the ids and deadlines under test are the
    ones the create route issued and stored (ARCHITECTURE.md Create).
    """
    fake_clock.instant = instant
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def _two_submissions(
    client: TestClient, fake_clock: FakeClock
) -> tuple[dict[str, str], dict[str, str]]:
    """The same text posted once at ``CREATED_AT`` and once an hour later."""
    first = _created_paste(client, fake_clock, REPEATED_TEXT)
    second = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=SECOND_CREATED_AT
    )
    return first, second


def test_the_same_text_posted_twice_answers_two_ids_and_two_links(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 10's first clause: two submissions, two ids (PRD.md item 9)."""
    first, second = _two_submissions(client, fake_clock)

    assert first["id"] != second["id"]
    assert first["url"] != second["url"]
    assert _paste_path(first) == f"{PASTES_PATH}/{first['id']}"
    assert _paste_path(second) == f"{PASTES_PATH}/{second['id']}"

    # No deduplication and no id reuse: the text is stored twice, once under
    # each id, each row carrying the instants of its own submission
    # (ARCHITECTURE.md Data: "no trimming, normalisation or deduplication").
    assert _stored_rows(db_path) == [
        (first["id"], REPEATED_TEXT, CREATED_AT, DEADLINE),
        (second["id"], REPEATED_TEXT, SECOND_CREATED_AT, SECOND_DEADLINE),
    ]
    assert _stored_text_bytes(_stored_rows(db_path)) == 2 * len(
        REPEATED_TEXT.encode("utf-8")
    )


def test_both_submissions_resolve_to_the_text_that_was_posted(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """Item 10's second clause: each link shows the text, byte for byte."""
    first, second = _two_submissions(client, fake_clock)

    for paste in (first, second):
        response = client.get(_paste_path(paste))

        assert response.status_code == 200, paste["id"]
        assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert response.content == REPEATED_TEXT.encode("utf-8")

    # The two answers are the same text rather than two different pastes that
    # happen to be similar, which is what makes the repeat a repeat.
    assert client.get(_paste_path(first)).content == client.get(
        _paste_path(second)
    ).content


def test_each_submission_dies_at_its_own_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Item 10's third clause: the two deadlines are separate (PRD.md item 4).

    At the first submission's deadline exactly one of the two links is gone and
    the other still serves the text; at the second's deadline both are gone.
    That pair is what rules out one shared lifetime for one text.
    """
    first, second = _two_submissions(client, fake_clock)

    fake_clock.instant = DEADLINE
    first_miss = client.get(_paste_path(first))
    second_still_live = client.get(_paste_path(second))

    assert first_miss.status_code == 404
    assert first_miss.content == NOT_FOUND_BODY
    assert first_miss.json() == {"error": NOT_FOUND_CODE}
    assert REPEATED_TEXT.encode("utf-8") not in first_miss.content

    assert second_still_live.status_code == 200
    assert second_still_live.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert second_still_live.content == REPEATED_TEXT.encode("utf-8")

    # The miss reclaimed the row it was asked for and left the twin's row, its
    # text and its own deadline in place (PRD.md item 6, ARCHITECTURE.md Read).
    assert _stored_rows(db_path) == [
        (second["id"], REPEATED_TEXT, SECOND_CREATED_AT, SECOND_DEADLINE)
    ]

    fake_clock.instant = SECOND_DEADLINE
    second_miss = client.get(_paste_path(second))

    assert second_miss.status_code == 404
    assert second_miss.content == NOT_FOUND_BODY
    assert REPEATED_TEXT.encode("utf-8") not in second_miss.content
    assert _stored_rows(db_path) == []


def test_a_dead_submission_does_not_take_its_twin_with_it(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """The two links are independent: one expiry reclaims one row, not both.

    After the first submission's deadline has passed, the second is still the
    text it was posted as — served over and over, with the stored bytes back to
    a single copy — and the dead id stays the uniform 404 rather than coming
    back (PRD.md items 5, 6 and 9).
    """
    first, second = _two_submissions(client, fake_clock)

    fake_clock.instant = DEADLINE
    assert client.get(_paste_path(first)).status_code == 404

    for _ in range(3):
        alive = client.get(_paste_path(second))
        assert alive.status_code == 200
        assert alive.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert alive.content == REPEATED_TEXT.encode("utf-8")

    rows = _stored_rows(db_path)
    assert _stored_ids(rows) == {second["id"]}
    assert _stored_text_bytes(rows) == len(REPEATED_TEXT.encode("utf-8"))

    again = client.get(_paste_path(first))
    assert again.status_code == 404
    assert again.content == NOT_FOUND_BODY
    assert _stored_ids(_stored_rows(db_path)) == {second["id"]}


def test_the_reported_deadlines_are_the_two_creation_instants_plus_the_lifetime(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Each submission reports, and each row stores, its own deadline (item 4).

    A deduplicating create route could hand the second caller the first
    submission's link and deadline; the reported instants here differ by
    exactly the hour between the two posts, and each one is the instant its own
    row holds.
    """
    first, second = _two_submissions(client, fake_clock)

    assert _reported_deadline(first) == DEADLINE
    assert _reported_deadline(second) == SECOND_DEADLINE
    assert _reported_deadline(second) - _reported_deadline(first) == ONE_HOUR_SECONDS

    stored_deadlines = sorted(row[3] for row in _stored_rows(db_path))
    assert stored_deadlines == [DEADLINE, SECOND_DEADLINE]
    assert sorted([_reported_deadline(first), _reported_deadline(second)]) == (
        stored_deadlines
    )


def test_both_expired_links_give_the_same_404_as_an_unknown_id(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """Item 9 with PRD.md item 5: a dead twin is as dead as an id never issued.

    Both submissions are past their own deadlines, so each link is answered by
    the app's one 404 handler: the same status, body and headers as each other
    and as an id that was never issued, with nothing of the repeated text in
    either answer.
    """
    first, second = _two_submissions(client, fake_clock)

    fake_clock.instant = SECOND_DEADLINE
    responses = {
        "first submission": client.get(_paste_path(first)),
        "second submission": client.get(_paste_path(second)),
        "unknown id": client.get(f"{PASTES_PATH}/{UNKNOWN_ID}"),
    }

    reference = responses["unknown id"]
    assert reference.status_code == 404
    assert reference.content == NOT_FOUND_BODY

    for case, response in responses.items():
        assert response.status_code == reference.status_code, case
        assert response.content == reference.content, case
        assert dict(response.headers) == dict(reference.headers), case
        assert REPEATED_TEXT.encode("utf-8") not in response.content, case
        assert "retry-after" not in response.headers, case


def test_two_submissions_at_one_instant_share_a_deadline_and_die_together(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """A repeat in the same instant is still two pastes with two identical deadlines.

    Both submissions are made with the clock held still, so the two rows carry
    the same ``expires_at``; that the deadlines coincide changes nothing but the
    arithmetic. Two ids, two links and two rows remain, and the shared deadline
    is still enforced per row: each link answers the uniform 404 at it (PRD.md
    items 4 and 9, and ``tests/test_store_contract.py`` tc013 for the same
    shared value at the store's own API).
    """
    first = _created_paste(client, fake_clock, REPEATED_TEXT)
    second = _created_paste(client, fake_clock, REPEATED_TEXT)

    assert first["id"] != second["id"]
    assert first["expires_at"] == second["expires_at"]
    assert _reported_deadline(first) == _reported_deadline(second) == DEADLINE

    rows = _stored_rows(db_path)
    assert _stored_ids(rows) == {first["id"], second["id"]}
    assert {row[3] for row in rows} == {DEADLINE}
    assert _stored_text_bytes(rows) == 2 * len(REPEATED_TEXT.encode("utf-8"))

    assert client.get(_paste_path(first)).content == REPEATED_TEXT.encode("utf-8")
    assert client.get(_paste_path(second)).content == REPEATED_TEXT.encode("utf-8")

    fake_clock.instant = DEADLINE
    misses = [client.get(_paste_path(paste)) for paste in (first, second)]

    for miss in misses:
        assert miss.status_code == 404
        assert miss.content == NOT_FOUND_BODY
        assert REPEATED_TEXT.encode("utf-8") not in miss.content

    assert _stored_rows(db_path) == []
