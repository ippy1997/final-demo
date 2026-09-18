# PRD: Pastebin API

## Goal

A developer who needs to hand a chunk of text to someone else — a stack trace, a config, a snippet — has no quick way to turn it into a link. The first version is a small HTTP service: a caller POSTs text and gets a link back, and anyone with that link opens it and sees the exact text. It replaces pasting large blobs into chat messages and email threads, and it stays out of the way: no account, no setup, one request in and one page out.

## Users

- **Paste creator.** A developer or teammate, or a script they wrote. Needs to send text and get back a link it can copy onward, in one request, without creating an account.
- **Paste reader.** Whoever the creator sends the link to, possibly with no tooling beyond a browser. Needs to open the link and see the text exactly as it was pasted — not a download, not a rendered approximation.
- **Service operator.** The developer running it on one host. Needs to start it with a single command, have pastes survive a restart, and have nothing else to run or maintain alongside it: no external database, queue or cloud account.

## First version

Each item is a behaviour with the check that decides it works.

1. **Create a paste.** A caller sends text to the create endpoint and receives a link back. *Works when:* a POST carrying text returns a success status whose body contains a link, and opening that link shows that text.
2. **Retrieve a paste exactly as submitted.** A GET on a paste's link returns the text verbatim. *Works when:* for bodies containing newlines, leading and trailing whitespace, non-ASCII characters and emoji, the response body equals the submitted text exactly, and the response is served as plain text so a browser displays it instead of downloading it.
3. **Links are unguessable and pastes are not enumerable.** *Works when:* ids carry at least 128 bits of randomness (default: 22-character URL-safe base64), 1000 pastes created back to back all have distinct ids, and no endpoint lists or searches pastes — a wrong but well-formed id returns 404 and never someone else's paste. This makes a link hard to find; it does not make a paste private (see Not in the first version).
4. **Unknown or malformed id returns 404.** *Works when:* a GET for an id that was never issued, and a GET for a malformed id, both return 404 with a short machine-readable error body — not a 500, and not an HTML stack trace.
5. **Empty and oversized input are rejected, and nothing is stored.** *Works when:* a POST with an empty body returns 400, a POST larger than the limit (default 1 MiB) returns 413, and after either the number of stored pastes is unchanged.
6. **Pastes survive a restart.** *Works when:* after stopping and restarting the service, a link created before the restart still returns its text.
7. **Repeated submissions are separate pastes.** *Works when:* posting the same text twice returns two different links and both resolve to that text — no deduplication, every create makes a new paste.

Together these cover the whole journey: creator POSTs text, the service stores it and returns a link, the creator passes the link on, and the reader opens it and sees the text — including after the service has been restarted. One end-to-end test walks that path from POST to displayed text.

## Not in the first version

- **Accounts, sign-in or API keys.** Anyone who can reach the service can create and read a paste. The link is the only credential.
- **Expiry, deletion and editing.** Pastes are kept indefinitely; removing one means manual work on the database by the operator. (See Open questions.)
- **Private pastes.** An unguessable link is not an access-controlled paste.
- **Listing, search, or "my pastes" views.**
- **Custom, memorable or human-chosen ids.**
- **Syntax highlighting, markdown or HTML rendering, and a separate raw-versus-rendered view.** Text is served as plain text.
- **A web form for creating pastes.** Creation is API-only; only reading has a browser view.
- **File uploads, multiple files per paste, images and binary content.**
- **Rate limiting, quotas, spam or abuse controls, and content moderation.**
- **Retention, backup or legal policy for stored text.**
- **Landing pages, branding and visual polish.**
- **Multi-instance or horizontally scaled operation.**

## Constraints

As stated by the developer:

- Python with FastAPI for the HTTP API.
- SQLite as the store.
- An automated test suite is part of the deliverable and covers the behaviours in First version.

## Success

- The test suite passes with a single command (default: `pytest`) and covers every numbered behaviour in First version, including the end-to-end create → link → read path. It needs no network access and does not touch the operator's live database.
- A manual end-to-end check succeeds: start the service, POST the contents of a real file with `curl`, then open the returned link from a fresh browser session and see the same text.
- A paste at the 1 MiB limit is returned in under 200 ms on the developer's machine (default, intended as "no obvious stall", not a benchmark).
- The service starts with one command as one process, and pastes still resolve after a restart.

## Defaults applied

Every decision below is mine, not the developer's. Each line says what would change if it is wrong. I asked about paste lifetime during this run and was told it had already been answered in a previous run; that answer is not in the repository (the checkout contains no files), so I applied the default and raised it under Open questions.

- **Pastes never expire and cannot be deleted or edited.** Wrong → adds a lifetime field and parameter, a delete path and a periodic sweep, changing the storage model and the tests.
- **No sign-in of any kind; the link is the only credential.** Wrong → adds accounts, paste ownership, and an authorization rule on every read.
- **Create returns JSON containing the link (`url`) and the id, with `url` absolute and built from the request's own host**, so the returned string can be pasted into a browser as-is. Wrong (a bare URL string, or a relative path) → only the create-response test changes.
- **Text is stored and served verbatim as UTF-8: no normalisation, trimming or deduplication.** Wrong → pastes stop round-tripping byte-for-byte, which is the point of capability 2.
- **Responses are `text/plain` with the text in the body; no HTML, highlighting or rendering.** Wrong → pastes gain a content-type or language field and the service gains a rendering layer.
- **Maximum paste size 1 MiB; an empty body is rejected with 400 and an oversized one with 413.** Wrong → one constant changes; only the boundary tests move.
- **Ids are 22-character URL-safe base64 over 128 random bits, with no custom ids.** Wrong → changes the id format and the uniqueness test.
- **Errors are JSON with a short machine-readable code and the appropriate status (400, 404, 413).** Wrong → error bodies change shape; the statuses should not.
- **The test suite is run with one command, without hand-starting a long-lived server, against a throwaway database rather than the operator's.** Wrong → tests need a running service and a prepared database file.
- **One host, one process, address configurable; no deployment work in the first version.** Wrong → needs packaging, a reverse proxy and a real hostname as separate work.

## Open questions

1. **Paste lifetime and deletion.** Default applied: never expire, no delete endpoint, no edit. This is the answer that most changes the storage model and the API surface, so it is worth confirming.
2. **Who can read a paste.** Default: anyone with the link, and nothing else protects it. Open: whether pastes must be restricted to an owner or a secret token, which brings sign-in back into scope.
3. **The 1 MiB ceiling.** Chosen as a safe default, not from anything you said. Confirm the intended limit.
4. **Reachability.** Default: the service runs locally or on one host you choose, so the link is only useful to people who can reach that host. Open: whether the first version must be reachable by others over the network from day one.
