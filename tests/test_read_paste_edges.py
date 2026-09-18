"""Task 7's read route at its edges (TASKS.md item 7).

``tests/test_read_paste.py`` walks the journey from the caller's side and
``tests/test_read_paste_contract.py`` states the task's clauses as acceptance
cases tc001-tc012. This module adds the cases those two leave open, each named
after the acceptance case it proves:

- tc101: a paste of exactly the 1 MiB ceiling reads back byte-for-byte as
  ``text/plain; charset=utf-8`` — item 2's verbatim round trip at the largest
  body item 7 lets in (PRD.md items 2 and 7).
- tc102: one second *past* the deadline the paste is the uniform 404 and its
  row is gone; the boundary case the task's own clause ("an expired paste, row
  deleted first") needs on the far side of the deadline (PRD.md item 5,
  ARCHITECTURE.md Read).
- tc103: each paste is enforced against the deadline stored in its own row, so
  at the older paste's deadline the older one 404s while a paste created two
  hours later still serves its own text (PRD.md item 4, ARCHITECTURE.md Data).
- tc104: the expired read deletes only the row it was asked for, measured in
  the throwaway database: the other paste's id, text and both instants are
  unchanged and the stored text bytes drop by exactly the expired text
  (PRD.md items 5 and 6).
- tc105: an id made of the URL-safe alphabet the ids module draws from,
  including ``-`` and ``_``, resolves through the link the create response
  returned (PRD.md item 3, ARCHITECTURE.md ids).
- tc106: a path that carries a valid id but an extra segment matches no route,
  so it is the *same* 404 as an id never issued, and the live paste keeps
  resolving (TASKS.md item 7, ARCHITECTURE.md Error contract).
- tc107: an id whose expired row housekeeping already removed reads as the same
  404 too: the uniformity must not depend on which code path removed the row
  (PRD.md item 5, ARCHITECTURE.md Sweep).

The instantaneous answer is the fixture's fake clock throughout, so no case
waits three hours or reads the machine's real clock (PRD.md Success). What the
documents do not decide — the trailing-slash path, and the real-server gate
with its machine-dependent latency — is recorded as tc108 and tc109 in the run
ledger and reported rather than asserted here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config, ids
from app.main import app

# The instants these cases work at: a creation instant, a second creation two
# hours later and the deadline the create route derives for the first
# (PRD.md item 4).
CREATED_AT = 1_700_000_000.0
TWO_HOURS = 2 * 60 * 60
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The documented path, content type and error contract (ARCHITECTURE.md Routes
# and Error contract).
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_CODE = "not_found"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# An id in the documented shape that was never issued (PRD.md item 3).
UNKNOWN_ID = "Z" * 22

# An id drawn from the URL-safe alphabet the ids module uses, holding both of
# the characters that are not alphanumeric (ARCHITECTURE.md ids decision row).
CHOSEN_URL_SAFE_ID = "-_" * 11

# The largest body item 7 accepts, which the read route must serve whole.
MAX_BYTES = config.MAX_PASTE_BYTES


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so "the row is really gone" and "the other row is really untouched" are
    measured on the database rather than on the answer (PRD.md items 5 and 6).
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def _paste_path(paste: dict[str, str]) -> str:
    """The path of the link the create response returned."""
    return urlsplit(paste["url"]).path


def _created_paste(
    client: TestClient, fake_clock: FakeClock, text: str, instant: float = CREATED_AT
) -> dict[str, str]:
    """Post ``text`` at ``instant`` and return the create response's body."""
    fake_clock.instant = instant
    response = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert response.status_code == 201, response.text
    return response.json()


def _stored_text_bytes(rows: list[tuple[str, str, float, float]]) -> int:
    """The UTF-8 size of the text in ``rows``, the unit item 6 counts in."""
    return sum(len(text.encode("utf-8")) for _, text, _, _ in rows)


def test_tc101_a_paste_at_the_size_ceiling_reads_back_byte_for_byte(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 2 at item 7's ceiling: the whole 1 MiB comes back unchanged."""
    text = "m" * MAX_BYTES
    assert len(text.encode("utf-8")) == MAX_BYTES
    paste = _created_paste(client, fake_clock, text)

    response = client.get(_paste_path(paste))

    assert response.status_code == 200
    assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert response.headers["content-length"] == str(MAX_BYTES)
    assert response.content == text.encode("utf-8")


def test_tc102_one_second_past_the_deadline_the_paste_is_gone(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 5: past the deadline the answer is the 404 and the row is gone."""
    text = "expired a second ago"
    paste = _created_paste(client, fake_clock, text)
    assert _stored_rows(db_path) != []

    fake_clock.instant = DEADLINE + 1
    response = client.get(_paste_path(paste))

    assert response.status_code == 404
    assert response.content == NOT_FOUND_BODY
    assert response.json() == {"error": NOT_FOUND_CODE}
    assert _stored_rows(db_path) == []
    assert text not in response.text


def test_tc103_each_paste_is_enforced_against_its_own_stored_deadline(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """PRD.md item 4: the deadline stored at creation decides, not the clock alone."""
    older_text = "created first, dies first"
    newer_text = "created two hours later"
    older = _created_paste(client, fake_clock, older_text)
    newer = _created_paste(client, fake_clock, newer_text, instant=CREATED_AT + TWO_HOURS)

    fake_clock.instant = DEADLINE

    expired = client.get(_paste_path(older))
    still_alive = client.get(_paste_path(newer))

    assert expired.status_code == 404
    assert expired.content == NOT_FOUND_BODY
    assert still_alive.status_code == 200
    assert still_alive.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert still_alive.content == newer_text.encode("utf-8")


def test_tc104_reading_an_expired_paste_deletes_only_its_own_row(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """Items 5 and 6: the expired text is reclaimed, and no other paste is touched."""
    read_text = "the paste that gets read past its deadline"
    kept_text = "the paste that stays in the table"
    read_paste = _created_paste(client, fake_clock, read_text)
    kept_paste = _created_paste(client, fake_clock, kept_text)
    rows_before = _stored_rows(db_path)
    assert len(rows_before) == 2

    fake_clock.instant = DEADLINE
    response = client.get(_paste_path(read_paste))

    assert response.status_code == 404
    assert response.content == NOT_FOUND_BODY
    assert _stored_rows(db_path) == [
        (kept_paste["id"], kept_text, CREATED_AT, DEADLINE)
    ]
    assert _stored_text_bytes(_stored_rows(db_path)) == len(kept_text.encode("utf-8"))

    # The paste that was not asked for is still there and still readable by the
    # same id, so the deletion went by id rather than by deadline.
    assert read_paste["id"] != kept_paste["id"]
    assert read_paste["id"] not in {row[0] for row in _stored_rows(db_path)}


def test_tc105_an_id_of_url_safe_characters_resolves_through_the_link(
    client: TestClient, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD.md item 3: the link resolves for the alphabet ids are drawn from."""
    text = "served under an id with - and _ in it"
    assert len(CHOSEN_URL_SAFE_ID) == 22
    assert "-" in CHOSEN_URL_SAFE_ID and "_" in CHOSEN_URL_SAFE_ID
    monkeypatch.setattr(ids, "new_id", lambda: CHOSEN_URL_SAFE_ID)

    paste = _created_paste(client, fake_clock, text)

    assert paste["id"] == CHOSEN_URL_SAFE_ID
    assert _paste_path(paste) == f"{PASTES_PATH}/{CHOSEN_URL_SAFE_ID}"

    response = client.get(_paste_path(paste))

    assert response.status_code == 200
    assert response.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
    assert response.content == text.encode("utf-8")


def test_tc106_an_extra_segment_after_a_valid_id_is_the_uniform_404(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """TASKS.md item 7: a path carrying an id but matching no route is the same 404."""
    text = "reachable only by its own link"
    paste = _created_paste(client, fake_clock, text)
    rows_before = _stored_rows(db_path)

    with_extra_segment = client.get(f"{PASTES_PATH}/{paste['id']}/extra")
    unknown_id = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert with_extra_segment.status_code == 404
    assert unknown_id.status_code == 404
    assert with_extra_segment.content == unknown_id.content == NOT_FOUND_BODY
    assert dict(with_extra_segment.headers) == dict(unknown_id.headers)
    assert text not in with_extra_segment.text

    # The live paste is untouched by the miss and still resolves on its own link.
    assert _stored_rows(db_path) == rows_before
    assert client.get(_paste_path(paste)).content == text.encode("utf-8")


def test_tc107_a_row_reclaimed_by_housekeeping_reads_as_the_uniform_404(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 5: which code path removed the row cannot change the answer."""
    text = "swept before anyone asked for it"
    paste = _created_paste(client, fake_clock, text)

    fake_clock.instant = DEADLINE
    swept = app.state.store.delete_expired(fake_clock.instant)

    assert swept == 1
    assert _stored_rows(db_path) == []

    response = client.get(_paste_path(paste))
    unknown_id = client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")

    assert response.status_code == 404
    assert response.content == unknown_id.content == NOT_FOUND_BODY
    assert dict(response.headers) == dict(unknown_id.headers)
    assert text not in response.text
