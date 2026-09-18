"""Task 10's repeated submissions at the edges its journey cases do not pin
(TASKS.md item 10).

TASKS.md item 10 is one line: the same text posted twice yields two ids, both
resolve to it, and each 404s on its own deadline (PRD.md item 9).
``tests/test_repeated_submissions.py`` walks that line — the two ids, both
links, both stored rows, the two reported deadlines, the two expired 404s and
two posts landing in the same instant. This module adds the edges those leave
open:

- the sweeper honours the two deadlines one crossing at a time, so the row of a
  repeated submission goes on its own deadline and only on it, while its twin
  stays stored, counted in the bytes and resolvable (PRD.md items 6 and 9);
- each submission draws its own id from ``ids.new_id()``: a create route that
  recognised a repeat would answer the second caller with the first
  submission's id without a second draw, so the generator is watched rather
  than assumed (ARCHITECTURE.md's decision row: ids are "never a counter, never
  derived from the text", and hashing "would also break item 9");
- submissions whose creation instants differ by a fraction of a second keep
  deadlines exactly that fraction apart, and each boundary is strict at its own
  instant (PRD.md item 4);
- reading both submissions repeatedly before their deadlines extends neither,
  and each still dies at its own deadline, one row at a time (PRD.md item 4);
- texts that differ only in whitespace are separate pastes: deduplication keyed
  on a trimmed or normalised text would collapse them, which item 9 forbids
  (PRD.md item 2 and its applied default: "no normalisation, trimming or
  deduplication");
- many submissions of one text are many pastes, not one row counted many times
  (PRD.md item 9);
- the same text posted again after the first copy was reclaimed gets a fresh
  id: nothing remembers the text, so a repeat is a new paste with a new
  deadline rather than a resurrection of the old one (PRD.md items 6 and 9).

Every submission goes through ``POST /pastes`` and every link is opened through
the path that response returned, so the ids and deadlines under test are the
ones the create route issued and stored (ARCHITECTURE.md Create and Read). The
clock is the fixture's ``FakeClock``, assigned between requests: no case waits
three hours and none reads the machine's real clock (PRD.md Success). The one
case that needs the sweeper to run asks for a short period indirectly, as
``tests/test_reclaim.py`` does, because the lifespan's loop reads that constant
when it takes each turn (``tests/conftest.py``).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config, ids

# The instants the repeated submissions are posted at, and the deadline the
# create route derives from the first (PRD.md item 4). The second submission
# lands an hour after the first; the case that needs the two instants to differ
# by a fraction of a second adds its own offsets below.
CREATED_AT = 1_700_000_000.0
ONE_HOUR_SECONDS = 60 * 60
QUARTER_SECOND = 0.25
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The period the sweeper runs at in the one case that has to watch a tick:
# short enough that several ticks happen while the case waits, long enough that
# the loop is not spinning (ARCHITECTURE.md Sweep).
FAST_SWEEP_INTERVAL_SECONDS = 0.01

# How long a case waits for an effect of the sweeper before calling it absent,
# and how often it looks: the sweeper runs in the app's own event loop, so its
# effect is the only thing a case can observe.
SWEEP_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.005

# The sweeper period, parametrized into ``sweep_interval`` for the case that
# has to watch ticks happen (tests/conftest.py).
FAST_SWEEP = pytest.mark.parametrize(
    "sweep_interval", [FAST_SWEEP_INTERVAL_SECONDS], indirect=True
)

# The documented path, the content type of a served paste and the error
# contract (ARCHITECTURE.md Routes and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# The one step below a deadline the strict boundary is probed at: short enough
# that a boundary rounded to the second would answer on the wrong side of it
# (PRD.md item 4's boundary default).
ONE_MICROSECOND = 1e-6

# How many times one text is posted in the case that measures the scale of a
# repeat: enough that a layout keeping one row per text, or losing submissions
# under a repeated key, would show.
SUBMISSIONS = 25

# The text every repeat carries. It holds the shapes PRD.md item 2 names, so an
# answer that re-encoded, trimmed or merged the submissions would be visible.
REPEATED_TEXT = "the same paste, posted again — é ☃ 😀\n\ttabbed\r\n  spaced  \n"

# Texts that differ only outside the visible characters: deduplication keyed on
# a trimmed or normalised text would treat them as one paste (PRD.md item 9 and
# its applied default of no normalisation or trimming).
WHITESPACE_VARIANTS = [
    "the same text, unwrapped",
    "the same text, unwrapped ",
    " the same text, unwrapped",
    "the same text, unwrapped\n",
]


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so "the twin is still stored" and "each row kept its own deadline" are
    measured on the database rather than on the answers (PRD.md item 9). WAL
    mode lets this read while the app's sweeper commits.
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
    """The ids in ``rows``, for asking which submission is left in the table."""
    return {row[0] for row in rows}


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned."""
    return urlsplit(paste["url"]).path


def _created_paste(
    client: TestClient,
    fake_clock: FakeClock,
    text: str,
    instant: float = CREATED_AT,
) -> dict[str, str]:
    """Post ``text`` with the clock set to ``instant``; return the response body.

    The clock is assigned rather than advanced, so a case that wants a
    submission made at an instant other than the default passes it here and the
    deadline stored on that row is derived from it (PRD.md item 4).
    """
    fake_clock.instant = instant
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def _wait_for(
    condition: Callable[[], bool], timeout: float = SWEEP_TIMEOUT_SECONDS
) -> bool:
    """Whether ``condition`` came true before ``timeout`` seconds had passed.

    The sweeper ticks in the app's own event loop, so a case observes it by
    waiting for its effect rather than by calling it; the wait ends as soon as
    the effect is there, and the condition is asked once more at the deadline
    so a slow machine is not reported as a missing sweep.
    """
    expiry = time.monotonic() + timeout
    while time.monotonic() < expiry:
        if condition():
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return condition()


@FAST_SWEEP
def test_the_sweeper_reclaims_each_submission_on_its_own_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md items 6 and 9: one text, two rows, one deadline each.

    Nothing reads either paste, so the sweeper is what reclaims them, and it
    reclaims them one crossing at a time: at the first submission's deadline
    that row and its bytes go while the twin stays stored and still resolves;
    only at the twin's own deadline does the table empty. One shared lifetime
    for the text, or one row per text, would fail on one side of that pair or
    the other.
    """
    second_created_at = CREATED_AT + ONE_HOUR_SECONDS
    second_deadline = second_created_at + config.PASTE_TTL_SECONDS

    first = _created_paste(client, fake_clock, REPEATED_TEXT)
    second = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=second_created_at
    )
    rows_at_creation = _stored_rows(db_path)
    assert _stored_ids(rows_at_creation) == {first["id"], second["id"]}
    assert _stored_text_bytes(rows_at_creation) == 2 * len(
        REPEATED_TEXT.encode("utf-8")
    )

    fake_clock.instant = DEADLINE

    # Only a running sweeper can remove this row: no link has been opened.
    assert _wait_for(lambda: first["id"] not in _stored_ids(_stored_rows(db_path)))

    rows = _stored_rows(db_path)
    assert rows == [
        (second["id"], REPEATED_TEXT, second_created_at, second_deadline)
    ]
    assert _stored_text_bytes(rows) == len(REPEATED_TEXT.encode("utf-8"))

    alive = client.get(_paste_path(second))
    assert alive.status_code == 200
    assert alive.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert alive.content == REPEATED_TEXT.encode("utf-8")

    fake_clock.instant = second_deadline

    assert _wait_for(lambda: _stored_rows(db_path) == [])

    assert _stored_rows(db_path) == []
    for paste in (first, second):
        miss = client.get(_paste_path(paste))
        assert miss.status_code == 404
        assert miss.content == NOT_FOUND_BODY
        assert miss.json() == {"error": NOT_FOUND_CODE}
        assert REPEATED_TEXT.encode("utf-8") not in miss.content


def test_each_submission_draws_its_own_id_from_the_generator(
    client: TestClient,
    db_path: Path,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARCHITECTURE.md's id decision row: a fresh draw per submission, no reuse.

    The generator is replaced by one that renders a different value on every
    call, so the ids in the two responses are the two draws: a create route
    that recognised a repeat of the same text would answer the second caller
    with the first submission's id — its row, its link and its deadline —
    without a second draw, and the second response would carry the first render
    instead.
    """
    drawn: list[str] = []

    def recording_new_id() -> str:
        identifier = f"drawn-{len(drawn):0>16}"
        assert len(identifier) == 22
        drawn.append(identifier)
        return identifier

    monkeypatch.setattr(ids, "new_id", recording_new_id)

    first = _created_paste(client, fake_clock, REPEATED_TEXT)
    second = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=CREATED_AT + ONE_HOUR_SECONDS
    )

    assert drawn == [first["id"], second["id"]]
    assert first["id"] != second["id"]
    assert [row[0] for row in _stored_rows(db_path)] == drawn
    assert client.get(_paste_path(first)).content == REPEATED_TEXT.encode("utf-8")
    assert client.get(_paste_path(second)).content == REPEATED_TEXT.encode("utf-8")


def test_submissions_a_fraction_of_a_second_apart_keep_deadlines_that_far_apart(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: each deadline is its own creation instant plus three hours.

    The two creation instants differ by a quarter of a second, so a deadline
    computed once, rounded to a whole second or shared between the two
    submissions would land on the wrong side of a microsecond probe at the
    earlier boundary.
    """
    first_created_at = CREATED_AT + QUARTER_SECOND
    second_created_at = CREATED_AT + 2 * QUARTER_SECOND
    first = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=first_created_at
    )
    second = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=second_created_at
    )

    first_deadline = first_created_at + config.PASTE_TTL_SECONDS
    second_deadline = second_created_at + config.PASTE_TTL_SECONDS
    assert second_deadline - first_deadline == QUARTER_SECOND

    fake_clock.instant = first_deadline - ONE_MICROSECOND
    just_before = client.get(_paste_path(first))

    fake_clock.instant = first_deadline
    at_first = client.get(_paste_path(first))
    twin_still_alive = client.get(_paste_path(second))

    assert just_before.status_code == 200
    assert just_before.content == REPEATED_TEXT.encode("utf-8")
    assert at_first.status_code == 404
    assert at_first.content == NOT_FOUND_BODY
    assert twin_still_alive.status_code == 200
    assert twin_still_alive.content == REPEATED_TEXT.encode("utf-8")

    fake_clock.instant = second_deadline - ONE_MICROSECOND
    twin_just_before = client.get(_paste_path(second))

    fake_clock.instant = second_deadline
    at_second = client.get(_paste_path(second))

    assert twin_just_before.status_code == 200
    assert twin_just_before.content == REPEATED_TEXT.encode("utf-8")
    assert at_second.status_code == 404
    assert at_second.content == NOT_FOUND_BODY


def test_repeated_reads_of_both_submissions_extend_neither_deadline(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: reading a repeat is still only reading.

    Both links are opened again and again across their lifetimes and neither
    writes anything: the two rows keep the text and the deadline creation gave
    them, and each still dies at its own instant, one row at a time
    (ARCHITECTURE.md Read).
    """
    second_created_at = CREATED_AT + ONE_HOUR_SECONDS
    second_deadline = second_created_at + config.PASTE_TTL_SECONDS

    first = _created_paste(client, fake_clock, REPEATED_TEXT)
    second = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=second_created_at
    )
    rows_at_creation = _stored_rows(db_path)

    read_instants = [CREATED_AT, CREATED_AT + 60, DEADLINE - 1, DEADLINE - 1]
    for instant in read_instants:
        fake_clock.instant = instant
        for paste in (first, second):
            response = client.get(_paste_path(paste))
            assert response.status_code == 200, (paste["id"], instant)
            assert response.content == REPEATED_TEXT.encode("utf-8")

    assert _stored_rows(db_path) == rows_at_creation

    fake_clock.instant = DEADLINE
    assert client.get(_paste_path(first)).content == NOT_FOUND_BODY
    assert client.get(_paste_path(second)).status_code == 200
    assert _stored_ids(_stored_rows(db_path)) == {second["id"]}

    fake_clock.instant = second_deadline
    assert client.get(_paste_path(second)).content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == []


def test_texts_that_differ_only_in_whitespace_are_separate_pastes(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md items 2 and 9: no trimming, no normalisation, so no collapsing.

    Four submissions whose texts differ only in leading, trailing or newline
    whitespace are four pastes under four ids, each serving exactly the text it
    was posted with. Deduplication keyed on a cleaned-up text — a plausible way
    to recognise a "repeat" — would store one row and answer four links with
    one text.
    """
    pastes = [
        _created_paste(client, fake_clock, text) for text in WHITESPACE_VARIANTS
    ]

    assert len({paste["id"] for paste in pastes}) == len(WHITESPACE_VARIANTS)

    rows = _stored_rows(db_path)
    assert _stored_ids(rows) == {paste["id"] for paste in pastes}
    assert [row[1] for row in rows] == WHITESPACE_VARIANTS

    for paste, text in zip(pastes, WHITESPACE_VARIANTS):
        response = client.get(_paste_path(paste))
        assert response.status_code == 200, paste["id"]
        assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert response.content == text.encode("utf-8")


def test_many_submissions_of_one_text_are_many_pastes(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 9 at scale: one text posted often is a paste per submission.

    Every submission answers its own id and its own link, every link resolves,
    the table holds one row per submission and the stored bytes are that many
    copies of the text. One row per text, one reused id or a dropped submission
    would show in any of those counts.
    """
    pastes = [
        _created_paste(client, fake_clock, REPEATED_TEXT) for _ in range(SUBMISSIONS)
    ]

    identifiers = [paste["id"] for paste in pastes]
    assert len(identifiers) == SUBMISSIONS
    assert len(set(identifiers)) == SUBMISSIONS

    rows = _stored_rows(db_path)
    assert _stored_ids(rows) == set(identifiers)
    assert len(rows) == SUBMISSIONS
    assert _stored_text_bytes(rows) == SUBMISSIONS * len(
        REPEATED_TEXT.encode("utf-8")
    )

    for paste in pastes:
        response = client.get(_paste_path(paste))
        assert response.status_code == 200, paste["id"]
        assert response.content == REPEATED_TEXT.encode("utf-8")

    # Every submission was made at the same instant, so all the deadlines
    # coincide; each link is still dead at it and the table empties as they are
    # opened (PRD.md items 4 and 9).
    fake_clock.instant = DEADLINE
    for paste in pastes:
        assert client.get(_paste_path(paste)).content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == []


def test_the_same_text_after_the_first_copy_was_reclaimed_is_a_new_paste(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 9: nothing about the text is remembered once its paste is gone.

    The first copy is read past its deadline and reclaimed. Posting the same
    text again at that instant answers a fresh id, not the reclaimed one: the
    new link resolves immediately and dies three hours after the new
    submission, so a repeat is a new paste rather than the old row being reused
    (PRD.md item 6).
    """
    resubmission_deadline = DEADLINE + config.PASTE_TTL_SECONDS

    reclaimed = _created_paste(client, fake_clock, REPEATED_TEXT)

    fake_clock.instant = DEADLINE
    first_miss = client.get(_paste_path(reclaimed))

    assert first_miss.status_code == 404
    assert first_miss.content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == []

    resubmitted = _created_paste(
        client, fake_clock, REPEATED_TEXT, instant=DEADLINE
    )

    assert resubmitted["id"] != reclaimed["id"]
    assert resubmitted["url"] != reclaimed["url"]
    assert _stored_rows(db_path) == [
        (resubmitted["id"], REPEATED_TEXT, DEADLINE, resubmission_deadline)
    ]

    served = client.get(_paste_path(resubmitted))
    assert served.status_code == 200
    assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert served.content == REPEATED_TEXT.encode("utf-8")

    # The old link is not resurrected by the resubmission, and the new one dies
    # at its own deadline rather than at the old paste's.
    assert client.get(_paste_path(reclaimed)).content == NOT_FOUND_BODY

    fake_clock.instant = resubmission_deadline
    assert client.get(_paste_path(resubmitted)).content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == []
