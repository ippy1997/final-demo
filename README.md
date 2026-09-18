# Pastebin

A small HTTP service that turns a chunk of text into a link: you POST the text, you get back a link and a deadline three hours later, and anyone with the link sees the text exactly as it was pasted until then. After the deadline the link is gone for good, and it answers with exactly the same 404 as a link that was never issued. One process, one SQLite file, no account and no setup.

**Not built yet.** The repository currently holds the plan: `PRD.md` (the agreed requirements), `ARCHITECTURE.md` (the design), `TASKS.md` (the build order) and this file. The application arrives with the tasks in TASKS.md; the commands below are what they will leave you with.

## Install

```
python -m pip install -r requirements-dev.txt
```

## Run

```
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Create a paste and read it back:

```
curl --data-binary @sample.txt http://127.0.0.1:8000/pastes
# 201 {"id": "...", "url": "http://127.0.0.1:8000/pastes/...", "expires_at": "..."}
curl http://127.0.0.1:8000/pastes/<id>          # 200, the text as plain text
curl -i http://127.0.0.1:8000/pastes/unknown    # 404 {"error":"not_found"}
```

The database file is `pastebin.db` in the working directory; set `PASTEBIN_DB` to put it elsewhere.

## Test

```
python -m pytest
```

The suite covers every behaviour in PRD.md's First version, including the create → link → read path, the identical 404 for expired, unknown and malformed ids, reclamation of expired text and restart survival. Expiry is tested with an injected clock, so nothing waits three hours and no test needs network access or the operator's database. For the design and the journeys to check by hand, see ARCHITECTURE.md.
