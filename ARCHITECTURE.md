# Architecture

Pastebin is one FastAPI process over one SQLite file, serving four routes. A create request stores the text with a deadline three hours ahead and answers with the link plus that deadline; a read request returns the text byte-for-byte before the deadline and, from the deadline on, the same 404 that a made-up id gets; expired rows are deleted on access and by a sweeper loop inside the same process. There is no second service, no queue, no cache, no external database and no account system: the link is the only credential. Every requirement in PRD.md is met by the parts below, and nothing else is built.

## Stack

- **Python 3.11+** — the PRD names Python; nothing here needs a newer runtime.
- **FastAPI** — the PRD names FastAPI for the HTTP API; its dependency overrides are also what makes the clock injectable, which the expiry tests need (PRD Success).
- **Uvicorn** — ASGI server; starts the whole application as one process with one command (PRD Users: service operator).
- **SQLite via the stdlib `sqlite3` module** — the PRD names SQLite; one file is what makes unexpired pastes survive a restart (item 8) and keeps the operator from running anything else. Rejected SQLAlchemy or an async driver: one table, one writer, no requirement for it.
- **pytest with httpx through FastAPI's test client** — a single-command suite that needs no network and never touches a live database (PRD Constraints, Success).
- **One asyncio task started in the FastAPI lifespan** — periodic deletion of expired rows, required by item 6 for pastes nobody reads again. Rejected: deletion only on access, which leaves never-read pastes stored forever; rejected: an OS cron job or systemd timer, a second moving part doing the same loop.

## Parts

| Part | File | Responsibility |
|---|---|---|
| HTTP app | `app/main.py` | routes, error handlers, lifespan start/stop of the sweeper |
| Config | `app/config.py` | database path, 3-hour TTL constant, 1 MiB limit, sweep interval |
| Clock | `app/clock.py` | `now()` time source, a FastAPI dependency tests override |
| Ids | `app/ids.py` | 128-bit URL-safe ids |
| Store | `app/store.py` | schema, insert/get/delete/sweep, the single SQLite connection |

Routes: `POST /pastes`, `GET /pastes/{paste_id}`, `GET /health`.

Error contract, one handler each, JSON body `{"error": "<code>"}`:
- `404 not_found` — expired paste, unknown id, malformed id, and any unmatched path: same status, same body, same headers, no `Retry-After`, no wording that hints the paste existed.
- `400 empty_body` — empty request body.
- `400 invalid_utf8` — request body is not valid UTF-8.
- `413 too_large` — request body over 1 MiB.

## Data

One table, no others:

```sql
CREATE TABLE IF NOT EXISTS pastes (
  id         TEXT PRIMARY KEY,
  text       TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pastes_expires_at ON pastes (expires_at);
```

`created_at` and `expires_at` are Unix seconds as floats; `expires_at = created_at + 10800` is computed once at creation and stored, so a restart or clock change cannot move a deadline (item 4). `text` is stored verbatim — no trimming, normalisation or deduplication (items 2, 9). No table for users, sessions or reads. Reclamation is measured as rows and text bytes in `pastes`; SQLite may keep the freed pages in the file, so the file on disk does not necessarily shrink (see open question 7).

Database file: `PASTEBIN_DB`, default `pastebin.db` in the working directory. Tests point it at a throwaway file.

## Journeys

**Create.** `POST /pastes` with the text as the raw body → 413 if `Content-Length` is over 1 MiB or the stream passes 1 MiB while reading → 400 if the body is empty or not valid UTF-8, nothing stored in either case → otherwise `id = ids.new()`, `now = clock.now()`, insert `(id, text, now, now + 3h)` → `201` with `{"id": ..., "url": "<request base URL>/pastes/<id>", "expires_at": "<ISO-8601 UTC>"}`.

**Read.** `GET /pastes/{paste_id}` → select by id → not found → uniform 404 → found and `clock.now() >= expires_at` → delete the row, then the same uniform 404 → otherwise `200` with `Content-Type: text/plain; charset=utf-8` and the stored text as the body, unchanged.

**Restart.** Startup reopens the same file. Deadlines are stored instants, so pastes inside their three hours still resolve, and one whose deadline fell during the downtime is swept or 404s on read.

**Sweep.** Every 60 s the lifespan task deletes rows with `expires_at <= clock.now()`. Tests call `store.delete_expired` directly instead of waiting.

## Sign-in

None, by requirement (PRD "Not in the first version": accounts, sign-in, API keys). The 22-character link is the only credential: anyone who can reach the host and holds the link can read the paste until its deadline. `/health` is unauthenticated too.

## Decisions

| Decision | Alternative rejected | Why |
|---|---|---|
| One FastAPI process plus one SQLite file | separate database server, queue or cache | the operator must run nothing else; items 1–9 fit in one process |
| Create body is raw text, read as UTF-8 whatever the content type | JSON envelope `{"text": ...}` | fewer steps for `curl --data-binary @file` and no escaping layer, which is the shortest path to "a caller sends text" |
| `expires_at` stored as an absolute instant | store `created_at` and compute at read | a restart or clock change must not shift a deadline (item 4) |
| One 404 handler for expired, unknown, malformed and unmatched paths | a distinct code or page per case | item 5 forbids any wording, body or header difference |
| Delete expired rows on read and in a sweeper | filter expiry at read time only | item 6: stored text must return to its previous level |
| Injectable `clock.now()`, default `time.time` | tests that sleep, or a configurable TTL | item 4 and Success: a controllable clock without waiting, and the lifetime stays unsettable |
| Strict boundary: retrievable while `now < expires_at`, 404 from `expires_at` | a one-second grace window | the PRD's boundary default; a grace would break item 5's promise |
| 404 latency not equalised | constant-time lookup paths | only body and status uniformity is required (PRD default); recorded as open question 3 |
| Ids from `secrets.token_urlsafe(16)` — 22 chars, 128 bits | counter, hash of the text, human-chosen ids | item 3: unguessable and non-enumerable; hashing would also break item 9 |
| Limits checked before or while reading the body, nothing inserted on 400 or 413 | validate after inserting | item 7: stored pastes unchanged |
| Synchronous `sqlite3`, one connection in WAL mode, access serialised in the app | async driver or connection pool | one process, one writer, one table; fewer moving parts |
| No `VACUUM` after sweeps | `VACUUM` on every sweep | item 6 counts rows and text bytes; `VACUUM` rewrites the file and needs spare disk |

## Verification

The first version is shown to work by the test suite and by one manual run.

Install and test, from the repository root:

```
python -m pip install -r requirements-dev.txt
python -m pytest
```

Start it and walk the journey:

```
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
curl --data-binary @sample.txt http://127.0.0.1:8000/pastes    # 201 with url and expires_at
curl http://127.0.0.1:8000/pastes/<id>                          # 200, the exact text, text/plain
curl -i http://127.0.0.1:8000/pastes/nope                       # 404 {"error":"not_found"}
```

Journeys the suite covers: create then read verbatim (newlines, leading and trailing whitespace, non-ASCII, emoji); retrievable at T+3h−1s and 404 at T+3h; repeated reads do not extend a deadline; expired, unknown and malformed ids give identical status, body and headers; stored rows and bytes return to baseline after the clock advances; restart keeps an unexpired paste and 404s one that died during the downtime; the same text twice gives two links with their own deadlines.

The platform gate below runs before each pull request. It is GET-only, so it proves startup and the uniform-404 path; creating a paste is covered by the suite and the manual run.

```verify
start: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
ready: http://127.0.0.1:8000/health
check: GET http://127.0.0.1:8000/health 200
check: GET http://127.0.0.1:8000/pastes/AAAAAAAAAAAAAAAAAAAAAA 404
check: GET http://127.0.0.1:8000/pastes/no-such-id 404
```

## Open questions

Each has a default applied, none blocks the first version, and each line says what changes if the default is wrong.

1. **Should the lifetime be settable by the caller or the operator?** Default: fixed 3 h for every paste, no create parameter, no configuration setting. Wrong → the create API gains a lifetime field, or config gains a setting, and with it boundary and validation tests.
2. **Strict boundary, or "about three hours" with housekeeping lag?** Default: strict — 404 from the deadline on. Wrong → a paste could answer briefly after its deadline, which conflicts with item 5.
3. **Must an expired lookup be indistinguishable in timing too?** Default: identical status, body and headers; latency not equalised. Wrong → both lookup paths get padded, constant-cost work.
4. **Who can read a paste?** Default: anyone with the link, until it expires. Wrong → an owner or token check on every read, and sign-in comes back into scope.
5. **Is 1 MiB the right ceiling?** Chosen as a default, not from the developer. Wrong → one constant and the boundary tests change.
6. **Must the link work for people beyond the host?** Default: local or one host of the developer's choosing. Wrong → packaging, a reverse proxy and a hostname become separate work.
7. **Should the database file shrink on disk?** Default: rows and text bytes are reclaimed, freed pages stay in the file. Wrong → add `VACUUM` or `incremental_vacuum` after sweeps.
