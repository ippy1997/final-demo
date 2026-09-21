"""TASKS.md item 1 acceptance cases for the health probe (``GET /health``).

TASKS.md item 1 ships the FastAPI app with ``GET /health`` returning 200, and
its success criterion is that the service starts with one command as one
process: the probe is what says the process is up. ARCHITECTURE.md fixes what
it is for — its Routes row lists ``GET /health`` beside the two paste routes,
its Verification block polls it as the readiness signal before it checks those
routes, and its Sign-in section records that it is unauthenticated like
everything else. ``tests/test_health.py`` (same task) already covers the
status, the exact payload and that the documented ASGI target serves the
route; these cases pin what makes it usable as a readiness signal, which no
case asserted yet:

- tc001: the answer does not depend on what is stored — the same status, body
  and headers before a paste exists and after one is created, so a monitor's
  verdict is about the process rather than about the database's contents
  (TASKS.md item 1, ARCHITECTURE.md Verification).
- tc002: the answer consults no clock, injected or real — the same 200 at the
  suite's fixed instant, at a paste's deadline and years past every deadline,
  with ``clock.time`` replaced by one that refuses to answer, so a readiness
  poll cannot fail because of the time (PRD.md Success: no case here reads the
  machine's clock).
- tc003: the probe stores nothing and moves nothing — the throwaway database
  holds the same rows with the same instants and the same text bytes after a
  burst of polls, so monitoring does not disturb the reclamation PRD.md item 6
  measures (ARCHITECTURE.md Sweep).
- tc004: the payload is the documented one and nothing else — the exact bytes
  ``{"status": "ok"}`` as ``application/json``, which is what a monitor parses
  — and the answer carries no header that belongs to a paste response
  (ARCHITECTURE.md Routes and Error contract).

What the probe deliberately is not is part of the contract too: it is not a
report on pastes, deadlines, the sweep or the store, so no case here asks it
for one, and none of the cases adds a query parameter or a header the
documents do not name.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import clock, config

# The documented path, payload and content type of the readiness probe
# (ARCHITECTURE.md Routes, TASKS.md item 1).
HEALTH_PATH = "/health"
HEALTH_BODY = {"status": "ok"}
HEALTH_BODY_BYTES = b'{"status":"ok"}'
JSON_CONTENT_TYPE = "application/json"

# The create path, used to give the probe something to ignore (ARCHITECTURE.md
# Create).
PASTES_PATH = "/pastes"

# The instant these cases create at and the deadline the create route derives
# from it (conftest's FIXED_INSTANT, PRD.md item 4).
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# The instants a monitor could poll at: with no paste yet, exactly at a paste's
# deadline, and far past every deadline the service will ever store.
PROBE_INSTANTS = [CREATED_AT, DEADLINE, CREATED_AT + 10 * 365 * 24 * 60 * 60]

# Headers that belong to an answer about a paste rather than to a readiness
# probe: a countdown to a deadline, and a document offered as a download
# (PRD.md items 2 and 5).
PASTE_ONLY_HEADERS = ("retry-after", "content-disposition")


class _ForbiddenClock:
    """Stands in for the ``time`` module and refuses to report an instant.

    Installed in place of ``clock.time`` to prove the probe answers from a
    constant rather than from the time: a readiness check that asked the
    machine for the time would raise here instead of answering (PRD.md
    Success).
    """

    def time(self) -> float:
        raise AssertionError("the real clock was read while a fake one was set")


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so "the probe wrote nothing" and "the deadline did not move" are
    measured on the database rather than on an answer from the service
    (PRD.md items 4 and 6).
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


def test_tc001_the_probe_answers_the_same_before_and_after_a_paste_is_stored(
    client: TestClient, fake_clock: FakeClock
) -> None:
    """TASKS.md item 1: readiness is a property of the process, not of the store."""
    empty = client.get(HEALTH_PATH)

    fake_clock.instant = CREATED_AT
    created = client.post(
        PASTES_PATH, content=b"a paste the readiness probe says nothing about"
    )
    assert created.status_code == 201, created.text

    stored = client.get(HEALTH_PATH)

    assert empty.status_code == stored.status_code == 200
    assert empty.json() == stored.json() == HEALTH_BODY
    assert empty.content == stored.content == HEALTH_BODY_BYTES
    assert dict(empty.headers) == dict(stored.headers)


def test_tc002_the_probe_consults_no_clock(
    client: TestClient,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD.md Success: the answer is the same at any instant, and reads no real clock."""
    monkeypatch.setattr(clock, "time", _ForbiddenClock())

    answers = []
    for instant in PROBE_INSTANTS:
        fake_clock.instant = instant
        answers.append(client.get(HEALTH_PATH))

    reference = answers[0]
    assert reference.status_code == 200
    assert reference.content == HEALTH_BODY_BYTES

    for instant, answer in zip(PROBE_INSTANTS, answers):
        assert answer.status_code == reference.status_code, instant
        assert answer.content == reference.content, instant
        assert dict(answer.headers) == dict(reference.headers), instant


def test_tc003_the_probe_stores_nothing_and_moves_nothing(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md items 4 and 6: polling reclaims nothing and creates nothing."""
    for _ in range(5):
        assert client.get(HEALTH_PATH).status_code == 200

    # Polled before anything was created, the probe left the store empty.
    assert _stored_rows(db_path) == []

    text = "the paste whose row and bytes the probe must not disturb"
    fake_clock.instant = CREATED_AT
    created = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert created.status_code == 201, created.text

    rows_before = _stored_rows(db_path)
    bytes_before = _stored_text_bytes(db_path)
    assert rows_before == [(created.json()["id"], text, CREATED_AT, DEADLINE)]
    assert bytes_before == len(text.encode("utf-8"))

    for _ in range(5):
        assert client.get(HEALTH_PATH).status_code == 200

    # Same rows, same stored deadline, same text: the probe is not a read path
    # that could reclaim or rewrite a paste (PRD.md items 4 and 6).
    assert _stored_rows(db_path) == rows_before
    assert _stored_text_bytes(db_path) == bytes_before


def test_tc004_the_answer_is_the_documented_payload_and_nothing_else(
    client: TestClient,
) -> None:
    """ARCHITECTURE.md Routes: one fixed body as JSON, with no paste header."""
    response = client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"] == JSON_CONTENT_TYPE
    assert response.content == HEALTH_BODY_BYTES
    assert response.json() == HEALTH_BODY
    assert set(response.json()) == {"status"}
    assert response.headers["content-length"] == str(len(HEALTH_BODY_BYTES))

    for header in PASTE_ONLY_HEADERS:
        assert header not in response.headers, header
