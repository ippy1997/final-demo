# PRD: Pastebin API

## Goal

A developer who needs to hand a chunk of text to someone else — a stack trace, a config, a snippet — has no quick way to turn it into a link. The first version is a small HTTP service: a caller POSTs text and gets a link back, and anyone with that link opens it and sees the exact text, for three hours. The link stops working three hours after it was made, and after that it is indistinguishable from a link that never existed, so pasted text does not sit around on the service forever. It replaces pasting large blobs into chat messages and email threads, and it stays out of the way: no account, no setup, one request in and one page out.

## Users

- **Paste creator.** A developer or teammate, or a script they wrote. Needs to send text and get back a link it can copy onward, in one request, without creating an account, and needs to know how long that link will last.
- **Paste reader.** Whoever the creator sends the link to, possibly with no tooling beyond a browser. Needs to open the link and see the text exactly as it was pasted — not a download, not a rendered approximation. If the link is past its three hours, they need to be told the paste is gone, without any hint of what it contained.
- **Service operator.** The developer running it on one host. Needs to start it with a single command, have unexpired pastes survive a restart, have nothing else to run or maintain alongside it (no external database, queue or cloud account), and have stored text bounded by the paste lifetime rather than growing forever.

## First version

Each item is a behaviour with the check that decides it works.

1. **Create a paste.** A caller sends text to the create endpoint and receives a link back. *Works when:* a POST carrying text returns a success status whose body contains a link, and opening that link shows that text.
2. **Retrieve a paste exactly as submitted.** A GET on a paste's link returns the text verbatim. *Works when:* for bodies containing newlines, leading and trailing whitespace, non-ASCII characters and emoji, the response body equals the submitted text exactly, and the response is served as plain text so a browser displays it instead of downloading it.
3. **Links are unguessable and pastes are not enumerable.** *Works when:* ids carry at least 128 bits of randomness (default: 22-character URL-safe base64), 1000 pastes created back to back all have distinct ids, and no endpoint lists or searches pastes — a wrong but well-formed id returns 404 and never someone else's paste. This makes a link hard to find; it does not make a paste private (see Not in the first version).
4. **Pastes expire three hours after creation.** The deadline is fixed when the paste is created and measures wall-clock time, so it does not reset on read, and service downtime or a restart does not extend it. *Works when:* with a controllable clock, a paste created at time T returns its text one second before T+3h and returns 404 at T+3h; a paste read repeatedly before its deadline still dies at T+3h; and a paste that was created before a restart and has not yet reached its deadline still resolves after the restart. The create response reports the deadline as an instant, so the creator knows how long the link is good for. No write of any kind to a paste changes its deadline.
5. **An expired paste returns the same 404 as an unknown id.** *Works when:* three GETs — an id never issued, a malformed id, and an id whose paste is past its three hours — each return the identical response: same status (404), same body (a short machine-readable error code), and no header, timing or wording difference that a caller could use to tell "this paste expired" from "this id never existed". In particular the expired paste's text is not returned, not partially returned, and not hinted at.
6. **Expired text is reclaimed, not just hidden.** *Works when:* with a controllable clock, after pastes pass their deadline the amount of stored text returns to what it was before those pastes were created, so a long-running service does not accumulate every paste ever made.
7. **Empty and oversized input are rejected, and nothing is stored.** *Works when:* a POST with an empty body returns 400, a POST larger than the limit (default 1 MiB) returns 413, and after either the number of stored pastes is unchanged.
8. **Unexpired pastes survive a restart.** *Works when:* after stopping and restarting the service, a link created before the restart and still inside its three hours returns its text — and one that reached its deadline during the downtime returns the same 404 as an unknown id.
9. **Repeated submissions are separate pastes.** *Works when:* posting the same text twice returns two different links, both resolve to that text, and the two die at their own deadlines three hours after their own creation — no deduplication, no shared lifetime.

Together these cover the whole journey: creator POSTs text, the service stores it and returns a link with the deadline, the creator passes the link on, the reader opens it and sees the text — and three hours later the same link gives the same 404 as a link that never existed. One end-to-end test walks that path from POST to displayed text to the identical 404.

## Not in the first version

- **Accounts, sign-in or API keys.** Anyone who can reach the service can create and read a paste. The link is the only credential.
- **Per-paste or operator-chosen lifetimes.** Every paste gets three hours; there is no lifetime parameter on create and no configuration setting for it.
- **Sliding, extendable or renewable expiry.** Reading a paste does not extend it, and there is no endpoint to extend, renew or re-issue a link.
- **Deletion, "burn after read" and editing.** A paste can only end by expiring; it cannot be removed earlier or changed.
- **Telling the reader that a paste expired.** No "this paste has expired" page, no countdown, no remaining-time header, no distinct status code, and no way to ask whether an id ever existed — the 404 is deliberately uniform.
- **Notifying the creator at expiry, and metrics on expired versus never-existed reads.**
- **Keeping expired text for recovery or audit, and exporting an expired paste or an operator archive.** Once the three hours are up, the text is gone.
- **Private pastes.** An unguessable link is not an access-controlled paste, and expiry does not make one.
- **Listing, search, or "my pastes" views.**
- **Custom, memorable or human-chosen ids.**
- **Syntax highlighting, markdown or HTML rendering, and a separate raw-versus-rendered view.** Text is served as plain text.
- **A web form for creating pastes.** Creation is API-only; only reading has a browser view.
- **File uploads, multiple files per paste, images and binary content.**
- **Rate limiting, quotas, spam or abuse controls, and content moderation.**
- **Retention, backup or legal policy for stored text beyond the three-hour lifetime.**
- **Landing pages, branding and visual polish.**
- **Multi-instance or horizontally scaled operation.**

## Constraints

As stated by the developer:

- Python with FastAPI for the HTTP API.
- SQLite as the store.
- An automated test suite is part of the deliverable and covers the behaviours in First version.
- Pastes expire three hours after creation.
- An expired paste returns the same 404 as an unknown id.

## Success

- The test suite passes with a single command (default: `pytest`) and covers every numbered behaviour in First version, including the end-to-end create → link → read path and the expired-paste 404.
- Expiry is tested without waiting three hours: time is injectable, so a test advances the clock past a paste's deadline and observes the 404 and the reclamation of stored text.
- A manual end-to-end check succeeds: start the service, POST the contents of a real file with `curl`, open the returned link from a fresh browser session and see the same text, then re-check the same link after the deadline and see the same 404 a made-up id gives.
- A paste at the 1 MiB limit is returned in under 200 ms on the developer's machine (default, intended as "no obvious stall", not a benchmark).
- The service starts with one command as one process; unexpired pastes still resolve after a restart.
- The test suite needs no network access and does not touch the operator's live database, and the expiry tests do not depend on the machine's real clock.

## Defaults applied

Every decision below is mine, not the developer's. Each line says what would change if it is wrong.

- **The three-hour lifetime is a fixed constant for every paste, taken from the wall clock at creation, with no create parameter and no operator setting, and downtime counts against it.** Wrong → adds a lifetime parameter (and its boundary tests) or a configuration setting, and for a downtime-excluded clock, an elapsed-time rule instead of a deadline.
- **The lifetime is counted from creation, not from first read, and reads never extend it.** The developer's wording pins the start at creation; the "not from first read" half is mine. Wrong → becomes a sliding lifetime, changing the storage model and the read path.
- **A paste's deadline is fixed at creation and stored, so the same instant is reported and enforced regardless of clock changes or restarts.** Wrong → expiry is recomputed per read and a restart could shift deadlines.
- **The create response carries the expiry instant (an absolute time) next to the link and id.** A caller whose link is temporary needs to know until when. Wrong → drop one field; only the create-response test changes.
- **The "identical 404" includes the body and status only; the service also avoids any distinguishing header or wording, but response latency is not required to be constant.** Wrong → if callers must not be able to time the difference, expired and unknown lookups need equalised work.
- **Expired text is deleted rather than kept behind a filter, with removal happening on access and by periodic housekeeping.** Wrong → storage grows without bound and the reclamation behaviour (item 6) is dropped.
- **Tests control the clock instead of sleeping, defaulting to an injectable time source.** Wrong → expiry tests become slow or racy, or the lifetime must be configurable to a test value.
- **Expiry does not change who can read a paste: the link is still the only credential, and a paste is world-readable until it dies.** Wrong → adds an owner or token check on every read, which brings sign-in back into scope.
- **Create returns JSON containing the link (`url`), the id, and the expiry instant, with `url` absolute and built from the request's own host**, so the returned string can be pasted into a browser as-is. Wrong (a bare URL string, or a relative path) → only the create-response test changes.
- **Text is stored and served verbatim as UTF-8: no normalisation, trimming or deduplication.** Wrong → pastes stop round-tripping byte-for-byte, which is the point of capability 2.
- **Responses are `text/plain` with the text in the body; no HTML, highlighting or rendering**, including on the error path. Wrong → pastes gain a content-type or language field and the service gains a rendering layer.
- **Maximum paste size 1 MiB; an empty body is rejected with 400 and an oversized one with 413.** Wrong → one constant changes; only the boundary tests move.
- **Ids are 22-character URL-safe base64 over 128 random bits, with no custom ids.** Wrong → changes the id format and the uniqueness test.
- **Errors are JSON with a short machine-readable code and the appropriate status (400, 404, 413); the expired-paste 404 uses the same code and wording as the unknown-id 404.** Wrong → error bodies change shape, but the statuses and the uniformity requirement in item 5 should not.
- **Boundary of the lifetime: a paste is retrievable strictly before its deadline and returns 404 at the deadline itself.** Wrong → shifts every expiry test by an instant.
- **The test suite is run with one command, without hand-starting a long-lived server, against a throwaway database rather than the operator's.** Wrong → tests need a running service and a prepared database file.
- **One host, one process, address configurable; no deployment work in the first version.** Wrong → needs packaging, a reverse proxy and a real hostname as separate work.

## Open questions

1. **Should the lifetime be settable by the caller or the operator?** Default applied: three hours, fixed for every paste, no create parameter and no configuration setting. Open: whether a creator picks a lifetime, or the operator tunes the default, which changes the create API and the set of accepted values.
2. **Should the boundary be strictly three hours, or "about three hours" with housekeeping lag?** Default applied: strict — retrievable strictly before the deadline, 404 from the deadline on. A housekeeping-driven reading would mean a paste could still answer for a short while after its deadline, which conflicts with item 5's promise.
3. **Should an expired lookup be indistinguishable in timing as well as in body?** Default applied: body and status identical, latency not equalised. Open: whether latency must be flattened too, which adds work to both lookup paths.
4. **Who can read a paste.** Default: anyone with the link, and nothing else protects it, for as long as it lives. Open: whether pastes must be restricted to an owner or a secret token, which brings sign-in back into scope.
5. **The 1 MiB ceiling.** Chosen as a safe default, not from anything you said. Confirm the intended limit.
6. **Reachability.** Default: the service runs locally or on one host you choose, so the link is only useful to people who can reach that host. Open: whether the first version must be reachable by others over the network from day one.
