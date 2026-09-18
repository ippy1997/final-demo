# AGENTS.md

What an engineering agent working in this repository needs. The agreed goal is `PRD.md`; the design is `ARCHITECTURE.md`; the build order is `TASKS.md`. Read those three before changing anything.

## Goal

PRD.md is the agreed requirements document and the thing every change is measured against. It is not edited by anyone working here, including you: if a requirement looks wrong or contradictory, raise it with the developer instead of rewriting it. Do not restate the requirements in code comments or docs — point at PRD.md.

## Current state

Only the plan is written: `PRD.md`, `ARCHITECTURE.md`, `TASKS.md`, `README.md`. The application described below does not exist yet; TASKS.md, in order, creates it.

## Stack

Python 3.11+, FastAPI, Uvicorn, stdlib `sqlite3` (one database file), pytest with httpx via FastAPI's test client, and one asyncio sweeper task inside the FastAPI lifespan. No queue, cache, external database, container or second process — each of those would need a requirement it cannot meet without, and the reasons for the choices are in ARCHITECTURE.md's Stack section.

## Layout

```
app/
  __init__.py
  main.py      # FastAPI app, routes, error handlers, lifespan (starts the sweeper)
  config.py    # TTL (3 h), 1 MiB limit, sweep interval, database path
  clock.py     # now(); the dependency tests override
  ids.py       # new_id(): 22-char URL-safe base64 over 128 random bits
  store.py     # SQLite schema and queries
tests/
  conftest.py  # temp database, fake clock, test client
  test_*.py    # one file per behaviour group
requirements.txt        # fastapi, uvicorn
requirements-dev.txt    # -r requirements.txt, pytest, httpx
```

## Conventions

- Errors are JSON `{"error": "<code>"}`. Every 404 — expired paste, unknown id, malformed id, unmatched path — goes through one handler and is byte-identical, per PRD item 5. Never add a header, code or wording that separates them.
- Time comes from `clock.now()` only. Never call `time.time()` in a route or in the store; tests advance the fake clock instead of sleeping, because the lifetime is fixed and unsettable (PRD item 4 and Success).
- A paste's `expires_at` is computed once at creation and stored; reads never write a deadline, and an expired row is deleted rather than filtered.
- Ids come from `ids.new_id()`; never a counter, never derived from the text.
- Text is stored and served verbatim as UTF-8. No trimming, normalisation or deduplication, and no rendering: responses are `text/plain; charset=utf-8`.
- The database path is read from `PASTEBIN_DB`; tests always use a temp file and never the operator's database.
- Tests need no network. Keep the suite in-process through the FastAPI test client.

## Install, run, test

From the repository root:

```
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m pytest
```

Once the tasks are done, `http://127.0.0.1:8000/health` answers as soon as the process is up. The gate the platform runs before each pull request, the journeys to check by hand, and the suite's coverage are listed in ARCHITECTURE.md's Verification section.
