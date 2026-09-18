"""Task 10's repeated submissions along their timeline: the repeat that arrives
around the earlier copy's death (TASKS.md item 10).

TASKS.md item 10 is one line: the same text posted twice yields two ids, both
resolve to it, and each 404s on its own deadline. PRD.md item 9 is what that
serves — "repeated submissions are separate pastes": two links, both resolving
to the text, dying at their own deadlines three hours after their own creation,
with no deduplication and no shared lifetime. ARCHITECTURE.md delivers it with
an id drawn per submission (its decision row: ``secrets.token_urlsafe(16)``,
never a counter and never derived from the text, because hashing "would also
break item 9") and a deadline computed once at creation and stored on the row,
so two rows share a text and nothing else.

``tests/test_repeated_submissions.py`` walks that line as one journey (two ids,
both links, both stored rows, the two reported deadlines, the two 404s, two
posts in the same instant) and ``tests/test_repeated_submissions_edges.py``
adds its edges (each row reclaimed by the sweeper on its own deadline, a fresh
generator draw per submission, fractional instants, reads extending neither
deadline, whitespace-only variants, many submissions, a resubmission after the
earlier copy was *read* and reclaimed). This module adds the cases those leave
open, named after the acceptance case each proves:

- tc1001: the repeat arrives at the very instant the earlier copy's deadline
  falls, while that copy's expired row is still in the table — nothing read it
  and no sweep can have run at the documented minute — and is a second paste
  with its own lifetime, not a reuse or a refresh of that row (PRD.md item 9).
- tc1002: with the clock moved backwards between the two submissions - a clock
  change, which PRD.md item 4 says cannot move a stored deadline - each
  deadline is still its own creation instant plus three hours, so the paste
  submitted second is the one that dies first (PRD.md item 4).
- tc1003: after both copies of the text have been read past their deadlines
  and reclaimed, a third submission of the same text is a third paste under a
  third id with a fresh three hours, and the two reclaimed ids stay dead
  (PRD.md items 6 and 9).

What the plan does not make testable is recorded as tc1004 in the run's case
list: two submissions genuinely in flight at once. The suite drives the app
through one synchronous test client, so the interleaving cannot be pinned and
would only measure the harness. The two things the plan does not decide and an
earlier task already records — the real sixty-second cadence and whether the
file shrinks on disk — are untestable here too.

Every submission goes through ``POST /pastes`` and every link is opened through
the path that response returned, so the ids and deadlines under test are the
ones the create route issued and stored (ARCHITECTURE.md Create and Read); the
stored rows are read through a second connection to the throwaway database the
app was started on, as ``tests/test_read_paste.py`` and
``tests/test_reclaim.py`` do. The clock is the fixture's ``FakeClock``,
assigned between requests: no case waits three hours and none reads the
machine's real clock (PRD.md Success). No case waits for a sweep either — the
documented minute is in force throughout, which is what makes "the expired row
is still stored" a measurement rather than a race.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from string import ascii_letters, digits
from urllib.parse import urlsplit

from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config

# The instants the submissions are posted at and the deadlines the create route
# derives from them (PRD.md item 4). The second submission lands an hour after
# the first, which is what makes the two deadlines distinguishable without
# waiting for either; tc1002 posts them in the opposite order, and tc1001 posts
# its repeat at the first copy's deadline itself and so derives that deadline
# from DEADLINE rather than from SECOND_CREATED_AT.
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

# The id shape ARCHITECTURE.md's ids decision row fixes: 22 characters over the
# URL-safe base64 alphabet. Checked on both ids of a repeat, so a create route
# that answered the second caller with a decorated or truncated variant of the
# first submission's id would fail rather than pass as "a different id".
ID_LENGTH = 22
URL_SAFE_ALPHABET = frozenset(ascii_letters + digits + "-_")

# The text every repeat carries. It holds the shapes PRD.md item 2 names, so an
# answer that re-encoded, trimmed or merged the submissions would be visible
# rather than coincidentally equal.
REPEATED_TEXT = "the same paste, submitted again — é ☃ 😀\n\ttabbed\r\n  spaced  \n"


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so "two rows, each with its own instants" and "only that row left the
    table" are measured on the database rather than on the answers (PRD.md
    item 9). WAL mode lets this read while the app may write.
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _stored_ids(db_path: Path) -> set[str]:
    """The ids in the table, for asking which submission is left in it."""
    return {row[0] for row in _stored_rows(db_path)}


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned.

    The url is absolute and built from the request's own host, so each case
    opens the path a browser would open rather than rebuilding one from the id
    (PRD.md applied defaults, ARCHITECTURE.md Create).
    """
    return urlsplit(paste["url"]).path


def _reported_deadline(paste: dict[str, str]) -> float:
    """The deadline the create response reported, as Unix seconds.

    PRD.md item 4 has the create response report the deadline as an instant, so
    the cases compare the instant each submission was told about rather than a
    number they recompute (ARCHITECTURE.md Create).
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


def test_tc1001_a_repeat_at_the_first_copys_deadline_is_a_second_paste(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 9: two ids, and the expired first row is not reused or refreshed.

    The repeat is submitted at the very instant the first copy's deadline falls.
    Nothing has read that copy and the sweeper's period is the documented
    minute, so its row is still in the table when the second submission
    arrives — the two reclamation paths ARCHITECTURE.md names are the read of
    that id and the sweep, and no create touches another paste's row
    (``tests/test_reclaim_edges.py`` tc916 pins the same absence of a third
    mechanism). The second submission must still be a paste of its own: its own
    id, its own link, and a deadline three hours after *its* creation, while the
    first row keeps the instants creation gave it.
    """
    repeat_deadline = DEADLINE + config.PASTE_TTL_SECONDS

    first = _created_paste(client, fake_clock, REPEATED_TEXT)
    assert _stored_rows(db_path) == [
        (first["id"], REPEATED_TEXT, CREATED_AT, DEADLINE)
    ]

    second = _created_paste(client, fake_clock, REPEATED_TEXT, instant=DEADLINE)

    # Two submissions, two ids in the documented shape, two links, and the
    # first row is neither reused nor rewritten: it still holds its own
    # creation instant and its own deadline, and the repeat carries a full
    # three hours from the instant it was made (ARCHITECTURE.md Data).
    assert second["id"] != first["id"]
    assert second["url"] != first["url"]
    assert _paste_path(first) == f"{PASTES_PATH}/{first['id']}"
    assert _paste_path(second) == f"{PASTES_PATH}/{second['id']}"
    for paste in (first, second):
        assert len(paste["id"]) == ID_LENGTH, paste["id"]
        assert set(paste["id"]) <= URL_SAFE_ALPHABET, paste["id"]

    assert sorted(_stored_rows(db_path)) == sorted(
        [
            (first["id"], REPEATED_TEXT, CREATED_AT, DEADLINE),
            (second["id"], REPEATED_TEXT, DEADLINE, repeat_deadline),
        ]
    )
    assert _reported_deadline(first) == DEADLINE
    assert _reported_deadline(second) == repeat_deadline

    # The expired copy is dead at that instant and reclaims only itself, while
    # the repeat that arrived with it still serves the text (PRD.md items 5
    # and 6).
    miss = client.get(_paste_path(first))

    assert miss.status_code == 404
    assert miss.content == NOT_FOUND_BODY
    assert miss.json() == {"error": NOT_FOUND_CODE}
    assert REPEATED_TEXT.encode("utf-8") not in miss.content
    assert sorted(_stored_rows(db_path)) == [
        (second["id"], REPEATED_TEXT, DEADLINE, repeat_deadline)
    ]

    alive = client.get(_paste_path(second))

    assert alive.status_code == 200
    assert alive.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert alive.content == REPEATED_TEXT.encode("utf-8")

    # And the repeat dies at its own deadline, not at the first copy's.
    fake_clock.instant = repeat_deadline
    second_miss = client.get(_paste_path(second))

    assert second_miss.status_code == 404
    assert second_miss.content == NOT_FOUND_BODY
    assert REPEATED_TEXT.encode("utf-8") not in second_miss.content
    assert _stored_rows(db_path) == []


def test_tc1002_a_repeat_created_earlier_dies_first(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: a deadline is the creation instant the clock reported, once.

    The same text is submitted twice and the clock is moved backwards between
    the two submissions — a corrected clock, which PRD.md item 4 and
    ARCHITECTURE.md Data both say cannot move a stored deadline. Each
    submission's deadline is therefore still its own creation instant plus the
    fixed three hours, which makes the *second* submission the one that dies
    first: the deadlines follow the instants the two rows hold, not the order
    the submissions arrived in. A lifetime computed at read time, or one shared
    between two pastes of one text, cannot produce that pair.
    """
    submitted_first = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=SECOND_CREATED_AT
    )
    submitted_second = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=CREATED_AT
    )

    assert submitted_second["id"] != submitted_first["id"]
    assert sorted(_stored_rows(db_path)) == sorted(
        [
            (
                submitted_first["id"],
                REPEATED_TEXT,
                SECOND_CREATED_AT,
                SECOND_DEADLINE,
            ),
            (submitted_second["id"], REPEATED_TEXT, CREATED_AT, DEADLINE),
        ]
    )

    # Each response reported its own deadline, an hour apart in the direction
    # of the creation instants rather than of the submission order.
    assert _reported_deadline(submitted_first) == SECOND_DEADLINE
    assert _reported_deadline(submitted_second) == DEADLINE
    assert (
        _reported_deadline(submitted_first) - _reported_deadline(submitted_second)
        == ONE_HOUR_SECONDS
    )

    # The paste submitted second reached its own deadline first.
    fake_clock.instant = DEADLINE
    second_miss = client.get(_paste_path(submitted_second))
    still_alive = client.get(_paste_path(submitted_first))

    assert second_miss.status_code == 404
    assert second_miss.content == NOT_FOUND_BODY
    assert REPEATED_TEXT.encode("utf-8") not in second_miss.content
    assert still_alive.status_code == 200
    assert still_alive.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert still_alive.content == REPEATED_TEXT.encode("utf-8")
    assert _stored_rows(db_path) == [
        (
            submitted_first["id"],
            REPEATED_TEXT,
            SECOND_CREATED_AT,
            SECOND_DEADLINE,
        )
    ]

    # The first submission keeps its own later deadline and dies at it.
    fake_clock.instant = SECOND_DEADLINE
    first_miss = client.get(_paste_path(submitted_first))

    assert first_miss.status_code == 404
    assert first_miss.content == NOT_FOUND_BODY
    assert REPEATED_TEXT.encode("utf-8") not in first_miss.content
    assert _stored_rows(db_path) == []


def test_tc1003_a_third_submission_after_both_copies_were_reclaimed_is_a_third_paste(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md items 6 and 9: nothing per text survives the pastes that held it.

    Both copies of the text are read past their deadlines, so both rows are
    reclaimed rather than filtered (ARCHITECTURE.md Read) and the table is
    empty. Posting the same text a third time at that instant has to answer a
    third id with its own three hours: a service that remembered the text — to
    deduplicate it, or to hand back an earlier link — would reuse or resurrect
    an id here, and the two ids that were reclaimed stay dead while the third
    is live.
    """
    first = _created_paste(client, fake_clock, REPEATED_TEXT)
    second = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=SECOND_CREATED_AT
    )
    assert _stored_ids(db_path) == {first["id"], second["id"]}

    fake_clock.instant = SECOND_DEADLINE
    for reclaimed in (first, second):
        response = client.get(_paste_path(reclaimed))
        assert response.status_code == 404, reclaimed["id"]
        assert response.content == NOT_FOUND_BODY
        assert REPEATED_TEXT.encode("utf-8") not in response.content
    assert _stored_rows(db_path) == []

    third = _created_paste(client, fake_clock, REPEATED_TEXT, instant=SECOND_DEADLINE)
    third_deadline = SECOND_DEADLINE + config.PASTE_TTL_SECONDS

    assert len({first["id"], second["id"], third["id"]}) == 3
    assert _stored_rows(db_path) == [
        (third["id"], REPEATED_TEXT, SECOND_DEADLINE, third_deadline)
    ]

    served = client.get(_paste_path(third))

    assert served.status_code == 200
    assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert served.content == REPEATED_TEXT.encode("utf-8")

    # The two reclaimed links are not resurrected by the third submission, and
    # the third does not inherit either of their deadlines.
    for reclaimed in (first, second):
        dead = client.get(_paste_path(reclaimed))
        assert dead.status_code == 404, reclaimed["id"]
        assert dead.content == NOT_FOUND_BODY
    assert client.get(_paste_path(third)).content == REPEATED_TEXT.encode("utf-8")

    fake_clock.instant = third_deadline
    assert client.get(_paste_path(third)).content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == []
