# Pastebin API

A small HTTP service that turns a chunk of text into a link: POST the text, get back
a link and a deadline. Three hours after creation the link stops working and the text
is deleted — an expired link answers the exact same 404 as one that never existed.

Built and tested. 286 tests, all passing.

## Run it

    python -m pip install -r requirements-dev.txt
    python -m pytest
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

## Use it

    curl --data-binary @some-file.txt http://127.0.0.1:8000/pastes
    # → 201 {"id": "...", "url": "http://.../pastes/<id>", "expires_at": ...}
    curl http://127.0.0.1:8000/pastes/<id>     # 200, the exact text, text/plain
    curl -i http://127.0.0.1:8000/pastes/nope  # 404 {"error":"not_found"}

Notes: no accounts or keys — the link is the only credential (ids carry 128 bits of
randomness). Pastes up to 1 MiB; empty input is rejected. Storage is one SQLite file
(`PASTEBIN_DB` relocates it, default `pastebin.db` in the working directory); pastes
survive restarts until their deadline. Package lives at `src/app/`, importing as `app`.

## Provenance

This application was built by governed AI agents on Agent Studio: 13 tasks, each
delivered as its own reviewed pull request, every agent commit cryptographically
sealed and verified by the `agent-studio/provenance` check.

Plan documents: `PRD.md` (requirements), `ARCHITECTURE.md` (design, including the
verify block), `TASKS.md` (build order), `AGENTS.md` (conventions).
