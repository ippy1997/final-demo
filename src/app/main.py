"""FastAPI application entry point: the app, its routes, its errors and the
lifespan that owns the store and the sweeper (ARCHITECTURE.md Parts, table row
"HTTP app").

TASKS.md builds this module in order: item 1 ships the app object and
``GET /health``, item 6 ships the create route below, item 7 adds the read
route and the one uniform 404 handler, and item 9 adds the sweeper to the
lifespan. What is here now is the skeleton plus the create journey, the read
journey and the sweeper that keeps the table from growing without bound.

**Create.** ``POST /pastes`` takes the text as the raw request body, whatever
its content type (ARCHITECTURE.md Create and its decision row "Create body is
raw text"): the body is read with the 1 MiB ceiling applied, so a request that
declares or streams more than ``config.MAX_PASTE_BYTES`` answers 413 without
the text being stored, and an empty body or one that is not valid UTF-8
answers 400 the same way (PRD.md item 7). The opt-in
``?burn_after_read=true`` query flag marks the paste for deletion on its first
successful read; it defaults to false, so a paste never burns just because a
link previewer or crawler opened it first. Only then is an id drawn from
``ids.new_id()``, the instant taken from the injected ``clock.now()``, the
deadline computed once from ``config.PASTE_TTL_SECONDS`` and the row handed to
the store (ARCHITECTURE.md Create). The answer is 201 with the id, the
absolute link built from this request and the deadline as an instant, which
is the response shape PRD.md's applied defaults fix.

**Read.** ``GET /pastes/{paste_id}`` looks the row up by its id through
``Store.consume`` and compares the stored deadline with the injected instant
(ARCHITECTURE.md Read). Strictly before the deadline the answer is 200 with
``Content-Type: text/plain; charset=utf-8`` and the stored text as the body,
unchanged, which is the verbatim round trip PRD.md item 2 asks for. If the
paste was created with the burn-after-read flag, the consume deleted the row
before that answer is written, so the first successful read is the only one
that can serve the text. From the deadline on the row is deleted before the
answer is written — expired text is removed rather than filtered (PRD.md item
6) — and the request answers with the 404 below.

**Sweep.** ``_sweep_expired_pastes`` is the one background task the process
runs: every ``config.SWEEP_INTERVAL_SECONDS`` it hands the current instant from
``clock.now()`` to ``store.delete_expired`` (ARCHITECTURE.md Sweep, TASKS.md
item 9). Without it, a paste nobody reads again would hold its text for the
life of the service, and PRD.md item 6 asks for the stored text to return to
its earlier level once the deadlines pass rather than to be merely hidden from
a reader. The lifespan starts the task at startup and cancels it before the
store is closed, so it never uses a connection that has gone away.

**The one 404.** ``_http_exception_handler`` is the single place the app
answers a 404: the read route raises for an id that was never issued, raises
again, after deleting the row, for a paste past its deadline, and the router
raises for a path no route matches. All three arrive as the same exception type
and leave as the same response — one status, one body, the same headers, and
nothing in the body, the wording or a header that could tell a caller which of
the three it was, which is PRD.md item 5 and ARCHITECTURE.md's error contract
(``404 not_found``). A status other than 404 keeps the handling FastAPI
installs by default.

The store is opened once by the lifespan, not per request: ``app.state.store``
is what the routes use, and it is closed on shutdown so a restart reopens the
same file with every unexpired paste in it (PRD.md item 8, ARCHITECTURE.md
Journeys: Restart). ``app.state.sweeper`` is the task started with it, which is
also how a test can see that the lifespan started one and stopped it. The
create route hands its insert to FastAPI's threadpool because that route reads
its request body asynchronously, and the read route is a synchronous route,
which FastAPI serves in the same threadpool: the store's one synchronous
connection is therefore never used on the event loop that serves the next
request (ARCHITECTURE.md Stack decision row). The sweeper runs in that event
loop and uses the same connection, which the store serialises with its own
lock.

Every error this module produces is JSON ``{"error": "<code>"}`` with a code
ARCHITECTURE.md's error contract names — ``not_found``, ``too_large``,
``empty_body``, ``invalid_utf8`` — and a rejected request reaches the store
nowhere: the create route checks the ceiling, the emptiness and the encoding
before inserting (PRD.md item 7) and the read route only deletes. The sweeper
is the one failure that is not an answer to a caller: a sweep that raises is
logged and the loop keeps going, because a database that was briefly locked
must not stop reclamation for the life of the process.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import clock, config, ids
from app.store import Store

# Where a failed sweep is reported. The lifespan's task has no request to fail
# and no caller to tell, so the log is the only place its trouble can surface.
logger = logging.getLogger(__name__)

# The two paths this module serves, in one place so the link the create
# response returns is the path the read route answers on (ARCHITECTURE.md
# Routes: `POST /pastes`, `GET /pastes/{paste_id}`).
PASTES_PATH = "/pastes"
PASTE_PATH = "/pastes/{paste_id}"

# The status and the code every 404 in the app carries (ARCHITECTURE.md Error
# contract: `404 not_found`). One pair of constants for the read route's
# rejection of an expired paste and for the handler that answers it, so the
# three 404s cannot drift apart (PRD.md item 5).
NOT_FOUND_STATUS = 404
NOT_FOUND_CODE = "not_found"

# What a retrieved paste is served as: plain text, with UTF-8 spelled out so a
# browser displays the text instead of downloading it and a caller never has to
# guess the encoding (PRD.md item 2, ARCHITECTURE.md Read).
TEXT_PLAIN_MEDIA_TYPE = "text/plain; charset=utf-8"


async def _sweep_expired_pastes(app: FastAPI) -> None:
    """Delete the expired pastes every ``config.SWEEP_INTERVAL_SECONDS``.

    The lifespan's one background task (ARCHITECTURE.md Parts, table row "HTTP
    app", and its Sweep journey): ``store.delete_expired`` is called with the
    current instant until the task is cancelled at shutdown, so a paste that
    nobody opens again still leaves the table and the stored text stays bounded
    by the paste lifetime rather than growing with every paste ever made
    (PRD.md item 6).

    The instant comes from ``clock.now()``, the same function the routes take as
    their dependency, so the process has one clock and the store reads none
    (AGENTS.md Conventions). The period is read from the configuration at each
    tick, so the documented constant is what decides it rather than a value
    captured at startup, and the boundary it enforces is the read path's
    boundary: a row goes from its deadline on, never a moment earlier.

    The first tick happens one interval after startup rather than at startup,
    so starting the service is not held up by a sweep; a paste whose deadline
    fell while the process was down is collected by the first tick or by the
    read that asks for it (ARCHITECTURE.md Journeys: Restart).

    A sweep that fails is logged and forgotten, and the loop carries on: a
    database that was briefly locked, or a file that went away, must not end
    reclamation for the rest of the process's life. ``CancelledError`` is not
    an ``Exception``, so the lifespan's shutdown still reaches this loop and
    ends it.
    """
    store: Store = app.state.store

    while True:
        await asyncio.sleep(config.SWEEP_INTERVAL_SECONDS)
        try:
            store.delete_expired(clock.now())
        except Exception:
            logger.exception(
                "the expired-paste sweep failed; the next tick will try again"
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the process's one store, run the sweeper, and close both at shutdown.

    The path comes from ``config.database_path()`` — ``PASTEBIN_DB`` or the
    working-directory default — and is read here rather than at import, which
    is what lets the test suite point the app at a throwaway file through the
    same environment variable the operator uses (ARCHITECTURE.md Data, PRD.md
    Success). Opening the store is also what creates the file, the table and
    the index if they are missing (TASKS.md item 5), so startup is the one
    place the app touches the filesystem.

    The sweeper is started here and cancelled here, before the store is
    closed: the lifespan owns the task, so there is no second way to start one
    and no task outliving the connection it uses (ARCHITECTURE.md Sweep,
    TASKS.md item 9).
    """
    store = Store(config.database_path())
    app.state.store = store
    sweeper = asyncio.create_task(_sweep_expired_pastes(app))
    app.state.sweeper = sweeper
    try:
        yield
    finally:
        # Cancelled and awaited before the store closes: the connection the
        # sweeper uses is the one the shutdown check-points back into the file,
        # so a task still running would query a closed database (PRD.md item 8).
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        store.close()


app = FastAPI(title="Pastebin", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _error(status_code: int, code: str) -> JSONResponse:
    """One error body, in the shape ARCHITECTURE.md's error contract fixes.

    The code is the short machine-readable one the contract names, so a caller
    distinguishes a rejected request by status and code alone (PRD.md item 7).
    """
    return JSONResponse(status_code=status_code, content={"error": code})


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Answer every 404 of the app with one identical response.

    This is the single handler behind all three ways a 404 happens here: an id
    that was never issued and, after its row is deleted, a paste whose deadline
    has passed are both raised by the read route, and a path no route matches
    is raised by the router. Each arrives as the same exception type, and a 404
    is answered from the constants above rather than from the exception's
    detail or from the request — nothing that separates the three cases (the
    path, the id, the case's own wording) can reach the response, which is what
    PRD.md item 5 forbids and ARCHITECTURE.md's error contract fixes as
    ``{"error": "not_found"}``.

    The request is deliberately unused: were the answer built from it, two of
    the three cases would be able to differ. A status that is not a 404 — a 405
    from the router, say — keeps the handling FastAPI installs by default, so
    overriding the 404 leaves every other error as it was.
    """
    if exc.status_code == NOT_FOUND_STATUS:
        return _error(NOT_FOUND_STATUS, NOT_FOUND_CODE)
    return await http_exception_handler(request, exc)


def _declared_content_length(request: Request) -> int | None:
    """What the request says its body weighs, or ``None`` if it does not say.

    A ``Content-Length`` over the ceiling lets the create route answer 413
    without reading the body at all (ARCHITECTURE.md Create). A missing,
    non-numeric or negative value is ignored here: the stream read below is
    what actually enforces the cap, and a header is a claim rather than a
    measurement.
    """
    declared = request.headers.get("content-length")
    if declared is None:
        return None
    try:
        length = int(declared)
    except ValueError:
        return None
    return length if length >= 0 else None


async def _read_body_within_limit(request: Request) -> bytes | None:
    """The raw request body, or ``None`` as soon as it passes the ceiling.

    The chunks are accumulated until the total would exceed
    ``config.MAX_PASTE_BYTES``; the read then stops immediately and the
    remaining body is never pulled, so an oversized request costs bounded
    memory and work rather than however much the caller chose to send (PRD.md
    item 7, ARCHITECTURE.md Create: "413 if ``Content-Length`` is over 1 MiB
    or the stream passes 1 MiB while reading"). The returned bytes are exactly
    what the caller sent — decoding, trimming and normalising are the route's
    business and it does none of them for the text it stores (PRD.md item 2).
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > config.MAX_PASTE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _paste_url(request: Request, paste_id: str) -> str:
    """The absolute link to a paste, built from this request's own host.

    ``request.base_url`` carries the scheme, the host and the port the caller
    reached the service on, so the returned string is complete enough to paste
    into a browser as-is rather than a path that only resolves next to the
    sender (PRD.md applied defaults; ARCHITECTURE.md Create).
    """
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}{PASTE_PATH.format(paste_id=paste_id)}"


def _iso8601_utc(instant: float) -> str:
    """A Unix instant as ISO-8601 in UTC, the form the create response reports.

    The deadline is an absolute instant, so it is rendered as one: the caller
    reads when the link stops working without having to know the service's
    timezone or the shape of the store (PRD.md item 4).
    """
    return datetime.fromtimestamp(instant, tz=timezone.utc).isoformat()


@app.post(PASTES_PATH, status_code=201)
async def create_paste(
    request: Request,
    now: clock.Now,
    burn_after_read: bool = False,
) -> JSONResponse:
    """Store the posted text and answer 201 with its id, link and deadline.

    The order is the requirement: the ceiling, then the empty and encoding
    checks, then the insert — so a rejected request stores nothing at all
    (PRD.md item 7). ``created_at`` is the injected instant and ``expires_at``
    is that instant plus the fixed lifetime, computed here once and stored, so
    a restart or a clock change cannot move a deadline (PRD.md item 4,
    ARCHITECTURE.md Data).

    ``burn_after_read`` is the explicit opt-in flag for a paste whose first
    successful GET deletes it. It is a query parameter, not a default: the
    route answers 201 for a plain ``POST /pastes`` exactly as before, and only
    a request that asks — ``POST /pastes?burn_after_read=true`` — stores the
    flag. That opt-in matters because a link previewer or crawler can issue the
    first GET; the creator, not the first opener, decides whether the paste is
    consumed by that open.
    """
    declared = _declared_content_length(request)
    if declared is not None and declared > config.MAX_PASTE_BYTES:
        return _error(413, "too_large")

    body = await _read_body_within_limit(request)
    if body is None:
        return _error(413, "too_large")

    if not body:
        return _error(400, "empty_body")

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return _error(400, "invalid_utf8")

    paste_id = ids.new_id()
    created_at = now
    expires_at = created_at + config.PASTE_TTL_SECONDS
    store: Store = request.app.state.store

    # The store's one connection is synchronous: it is used off the event loop
    # so a write cannot stall the requests served while it commits.
    await run_in_threadpool(
        store.insert,
        paste_id,
        text,
        created_at,
        expires_at,
        burn_after_read,
    )

    return JSONResponse(
        status_code=201,
        content={
            "id": paste_id,
            "url": _paste_url(request, paste_id),
            "expires_at": _iso8601_utc(expires_at),
        },
    )


@app.get(PASTE_PATH)
def read_paste(paste_id: str, request: Request, now: clock.Now) -> Response:
    """Answer the stored text, or the uniform 404 from the deadline on.

    The lookup is ``Store.consume``: it hands back the row with the deadline it
    was created with, and for a paste whose creator opted into burn-after-read
    it deletes the row in the same lock before this route can answer 200. That
    makes the first successful read the only one that receives the text; a
    racing or later reader gets ``None`` and the same 404 as an id that was
    never issued. An ordinary paste is returned without a write, exactly as a
    plain lookup.

    The injected instant decides the boundary: while ``now`` is strictly before
    ``expires_at`` the answer is 200 with ``text/plain; charset=utf-8`` and the
    stored text as the body, unchanged (PRD.md items 2 and 4). From the
    deadline on the row is deleted before the request answers, so expired text
    is reclaimed rather than left behind a filter (PRD.md item 6), and the
    answer is the app's one 404 — the same status, body and headers an id that
    was never issued gets, with no hint that this paste ever existed (PRD.md
    item 5, ARCHITECTURE.md Read).

    The route is synchronous, so FastAPI serves it in its threadpool and the
    store's one synchronous connection is used off the event loop, as the
    create route's insert is (ARCHITECTURE.md Stack decision row). Raising
    rather than returning the 404 is what puts this route's rejections through
    the single handler above, together with the router's unmatched paths.
    """
    store: Store = request.app.state.store
    paste = store.consume(paste_id)

    if paste is None:
        # Never issued, or issued and already consumed or deleted — by an
        # earlier burn-after-read, by this route, or by the sweeper: the same
        # uniform 404 either way (PRD.md item 5).
        raise StarletteHTTPException(
            status_code=NOT_FOUND_STATUS, detail=NOT_FOUND_CODE
        )

    if now >= paste.expires_at:
        # Deleted first, then the 404: the expired text is gone rather than
        # hidden (PRD.md item 6), and the row has already left the table by the
        # time the answer is written. A burn-after-read row that reached its
        # deadline before being read was already deleted by ``consume``; an
        # ordinary expired row is deleted here. Deleting by id cannot disturb
        # another paste, and an id the sweeper has already collected deletes
        # nothing.
        if not paste.burn_after_read:
            store.delete(paste_id)
        raise StarletteHTTPException(
            status_code=NOT_FOUND_STATUS, detail=NOT_FOUND_CODE
        )

    # The text is served as it is stored: no trimming, no normalisation, no
    # rendering (PRD.md item 2, ARCHITECTURE.md Read).
    return PlainTextResponse(content=paste.text, media_type=TEXT_PLAIN_MEDIA_TYPE)
