"""The readiness probe at the edges its two existing modules leave open
(TASKS.md item 1).

``tests/test_health.py`` pins TASKS.md item 1 from the caller's side — 200,
``application/json``, the exact payload, and that the documented ASGI target
``app.main:app`` serves the route — and ``tests/test_health_contract.py`` pins
what makes it usable as a readiness signal: the same answer before and after a
paste is stored, no clock consulted, nothing written, and the payload as the
documented bytes with no paste-only header. This module adds the three cases
those leave open, each named after the acceptance case it proves:

- TC-201: the probe answers with the store *unusable* — ``app.state.store``
  replaced by an object that refuses every attribute access — so "readiness is
  about the process, not about the store" is proven for the read side too and
  not only for the write side (app/main.py, ``health``: it touches neither
  ``clock.now()`` nor ``app.state.store``).
- TC-202: an expired row whose reclamation is already due is left exactly where
  it is by a burst of polls: the sweeper and the read path are the only two
  things that reclaim text (PRD.md item 6, ARCHITECTURE.md Sweep and Read), so
  a probe that swept opportunistically would be doing work off a monitor's GET.
- TC-203: a poll carrying a credential this service never issued is answered
  like one carrying none, because ``/health`` is unauthenticated like every
  route here (ARCHITECTURE.md Sign-in) and the gate reaches it with a plain
  GET and no credential (ARCHITECTURE.md Verification).

Every case is driven through the fixture's ``client`` and ``FakeClock``
(``tests/conftest.py``), so none of them opens a socket, reads the machine's
clock or waits for a sweep: the documented minute is left in force, which is
why no tick can be the cause of anything observed here. What the documents do
not decide — the trailing-slash path, HEAD, query parameters, and the
live-server poll of the platform gate's ``ready:``/``check:`` lines — is not
asserted; the gate's poll is recorded as untestable in the run's case list
because it needs a running uvicorn process and a socket, which the suite must
not require (AGENTS.md Conventions, PRD.md Constraints).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient

from app import config
from app.main import app

# The documented path, payload and content type of the readiness probe
# (ARCHITECTURE.md Routes, TASKS.md item 1).
HEALTH_PATH = "/health"
HEALTH_BODY = {"status": "ok"}
HEALTH_BODY_BYTES = b'{"status":"ok"}'
JSON_CONTENT_TYPE = "application/json"

# The create and read paths, the instant a case stores at and the deadline the
# create route derives from it (ARCHITECTURE.md Create, PRD.md item 4).
PASTES_PATH = "/pastes"
CREATED_AT = 1_700_000_000.0
DEADLINE = CREATED_AT + config.PASTE_TTL_SECONDS

# An id in the documented shape that was never issued, used to show a route that
# does use the store is stopped by the unusable one (PRD.md item 3).
UNKNOWN_ID = "Z" * 22

# How many times a monitor could reasonably poll while an expired row is
# waiting for reclamation.
POLL_COUNT = 5

# Credentials this service never issued, of the shapes a caller or a probe
# front-end might attach to its poll: a bearer token, a session cookie and an
# API key header. None of them may change the answer (ARCHITECTURE.md
# Sign-in: no sign-in exists, ``/health`` included).
WRONG_CREDENTIAL_HEADERS = [
    pytest.param(
        {"authorization": "Bearer not-a-token-this-service-ever-issued"},
        id="bearer-token",
    ),
    pytest.param(
        {"cookie": "session=not-a-session-this-service-ever-issued"},
        id="session-cookie",
    ),
    pytest.param(
        {"x-api-key": "not-an-api-key-this-service-ever-issued"},
        id="api-key",
    ),
]


class _UnusableStore:
    """Stands in for the store and refuses to answer anything at all.

    Installed over ``app.state.store`` while the app is running, so the probe is
    asked its question in a state the store cannot reply in at all — a failure
    the module's own docstring says the probe's answer cannot depend on ("it
    takes no dependency at all — not the store, not the clock"). Every
    attribute access raises, so an implementation that counted rows, asked for
    the text bytes or even looked at a schema version would fail here instead
    of answering (app/main.py, ``health``).
    """

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the readiness probe reached for the store: {name!r}")


def _stored_rows(db_path: Path) -> list[tuple[str, str, float, float]]:
    """Every stored paste as ``(id, text, created_at, expires_at)``.

    Read through a second connection to the throwaway file the app was started
    on, so "the probe reclaimed nothing and moved nothing" is measured on the
    database rather than on an answer from the service (PRD.md items 4 and 6).
    """
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, text, created_at, expires_at FROM pastes"
        ).fetchall()
    finally:
        connection.close()


def test_tc201_the_probe_answers_while_the_store_cannot_be_used_at_all(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """app/main.py ``health``: the readiness answer takes no store dependency.

    The answer is taken once with the real store in place and once with a store
    that raises on every access: a monitor polling during a database outage has
    to get the same thing it gets during normal operation, which is what makes
    the probe a check on the process rather than a second read path
    (ARCHITECTURE.md Verification polls it before it checks the paste routes).
    """
    with_the_real_store = client.get(HEALTH_PATH)
    assert with_the_real_store.status_code == 200

    monkeypatch.setattr(app.state, "store", _UnusableStore())

    without_a_usable_store = client.get(HEALTH_PATH)

    assert without_a_usable_store.status_code == 200
    assert without_a_usable_store.headers["content-type"] == JSON_CONTENT_TYPE
    assert without_a_usable_store.content == HEALTH_BODY_BYTES
    assert without_a_usable_store.json() == HEALTH_BODY
    assert dict(without_a_usable_store.headers) == dict(with_the_real_store.headers)

    # The substituted store is the one the running app consults: a route that
    # does use it is stopped by it, so the 200 above is an answer given without
    # the store rather than a patch the app never saw (ARCHITECTURE.md Read).
    with pytest.raises(AssertionError, match="store"):
        client.get(f"{PASTES_PATH}/{UNKNOWN_ID}")


def test_tc202_an_expired_row_waiting_for_reclamation_survives_a_burst_of_polls(
    client: TestClient, db_path: Path, fake_clock: FakeClock
) -> None:
    """PRD.md item 6 with app/main.py ``health``: polling reclaims nothing.

    The row here is past its deadline, so reclamation is due — but only the
    read path and the sweeper may do it (ARCHITECTURE.md Read and Sweep), and
    the documented minute is in force, so no tick runs during the polls. The
    row, its text and the deadline stored at creation are therefore still there
    afterwards, byte for byte, and the read that follows both proves the row
    really was expired and reclaims it, as item 6 asks.
    """
    text = "expired while nobody looked, and not the readiness probe's business"
    fake_clock.instant = CREATED_AT
    created = client.post(PASTES_PATH, content=text.encode("utf-8"))
    assert created.status_code == 201, created.text
    paste_id = created.json()["id"]

    fake_clock.instant = DEADLINE
    rows_before = _stored_rows(db_path)
    assert rows_before == [(paste_id, text, CREATED_AT, DEADLINE)]

    answers = [client.get(HEALTH_PATH) for _ in range(POLL_COUNT)]

    assert [answer.status_code for answer in answers] == [200] * POLL_COUNT
    assert {answer.content for answer in answers} == {HEALTH_BODY_BYTES}
    assert _stored_rows(db_path) == rows_before

    # The row was genuinely past its deadline: the read reclaims it and answers
    # the uniform 404, so the polls above left an expired paste in the table
    # rather than a live one (PRD.md items 5 and 6).
    reclaimed = client.get(f"{PASTES_PATH}/{paste_id}")

    assert reclaimed.status_code == 404
    assert _stored_rows(db_path) == []


@pytest.mark.parametrize("headers", WRONG_CREDENTIAL_HEADERS)
def test_tc203_a_poll_with_a_wrong_credential_is_answered_like_one_without(
    client: TestClient, headers: dict[str, str]
) -> None:
    """ARCHITECTURE.md Sign-in: ``/health`` is unauthenticated, so no credential decides.

    A service that grew a sign-in check on its probe would turn away the very
    monitors ARCHITECTURE.md's Verification block relies on: the answer to a
    poll carrying a token, a cookie or an API key this service never issued has
    to be the answer to a poll carrying nothing, with no challenge header to
    send a monitor looking for one.
    """
    anonymous = client.get(HEALTH_PATH)
    assert anonymous.status_code == 200

    credentialed = client.get(HEALTH_PATH, headers=headers)

    assert credentialed.status_code == 200
    assert credentialed.headers["content-type"] == JSON_CONTENT_TYPE
    assert credentialed.content == HEALTH_BODY_BYTES
    assert credentialed.json() == HEALTH_BODY
    assert "www-authenticate" not in credentialed.headers
    assert dict(credentialed.headers) == dict(anonymous.headers)
