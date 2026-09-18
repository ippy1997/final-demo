"""Task 13: the run path — one command, the documented gate, the README
commands and a 1 MiB paste's answer time (TASKS.md item 13).

TASKS.md item 13 is one line: ``python -m uvicorn app.main:app --host
127.0.0.1 --port 8000`` starts clean, the ARCHITECTURE.md verify block passes,
the README commands match, and a 1 MiB paste answers in under 200 ms on this
machine. Its success criterion is the operator's half of PRD.md's Success: "the
service starts with one command as one process", the manual end-to-end check
("start the service, POST the contents of a real file with ``curl``, open the
returned link ... and see the same text"), and "a paste at the 1 MiB limit is
returned in under 200 ms on the developer's machine (default, intended as 'no
obvious stall', not a benchmark)").

Every other module in the suite drives the app in-process through FastAPI's
test client, which is what makes the suite one command with nothing to start
first and no network (AGENTS.md Conventions, PRD.md Success). This module is
the one place that has to do the opposite, because what it has to show is a
property of the run path itself and no in-process client can show it: that the
documented command starts a process which imports the documented ASGI target,
opens its store, answers the documented gate over a real socket, and stops
cleanly when the operator ends it. So each case starts the documented command
as a child process through ``_running_service`` — on a throwaway database and
an ephemeral port — and talks to it with ``httpx``, which the suite already
depends on. The application under test is the same ``app.main:app`` the rest of
the suite serves, and the process is started and stopped inside the case that
needs it, so nothing is left running behind the suite.

Where the run path differs from the suite's other cases, and why:

- **The real clock.** The documented command installs no clock override, so a
  process started this way works in real instants. The expiry boundary is
  therefore not crossed here — that is the injected-clock cases' job (TASKS.md
  items 8, 9 and 12) — and the one case that needs a dead link seeds a row whose
  deadline is already an hour behind (``_seed_pastes``) rather than waiting
  three hours (PRD.md Success, TASKS.md item 13).
- **An ephemeral port.** The documented command names port 8000; the cases run
  the same command with ``--port 0`` and read the port uvicorn reports, so the
  suite cannot collide with anything else listening on the developer's machine.
  Host, target and flags are the documented ones, and tc1305 checks the command
  this module runs against the command the documents give.
- **The verify block is read, not copied.** tc1301 parses ARCHITECTURE.md's
  ```verify block — the gate the platform runs before each pull request — and
  replays its ``start``, ``ready`` and ``check`` lines against the running
  process, so the block and the service cannot drift apart unnoticed.

What the cases pin:

- tc1301: the documented start command starts one process cleanly — no
  traceback, no import failure, the readiness probe the block names answering
  200, and its store in the throwaway file the case set — and every ``check``
  line of the verify block gets the status it documents (TASKS.md item 13,
  ARCHITECTURE.md Verification).
- tc1302: the manual end-to-end check, walked against the running process: POST
  the text, open the absolute link the answer returned and see the text
  byte-for-byte as ``text/plain; charset=utf-8``, with the row in the throwaway
  file carrying the creation instant plus the fixed three hours (PRD.md items
  1, 2 and 4, PRD.md Success).
- tc1303: the four ways a read misses — a paste whose deadline has passed, an
  id that was never issued, a malformed id and a path no route matches — answer
  one identical 404 from the running process; the expired row is deleted rather
  than hidden, and a paste still inside its three hours is untouched (PRD.md
  items 5 and 6).
- tc1304: a paste of exactly the 1 MiB ceiling round-trips through the running
  process and is answered inside the documented 200 ms, on both the create and
  the read side (PRD.md item 7's ceiling, PRD.md Success's latency default).
- tc1305: the commands the documents give are the commands this module runs:
  ARCHITECTURE.md's verify block and README.md name the same target, host and
  flags, README.md names the install and test commands, and README.md names the
  database variable and default filename the code reads (TASKS.md item 13).
- tc1306: stopping the process the way an operator does — a signal, not a kill
  — is a clean shutdown: the lifespan's shutdown finishes, the exit status is
  the signal after uvicorn's graceful handling, nothing raised, and the paste is
  in the throwaway file afterwards, which is the restart survival of PRD.md
  item 8 measured on the real process.

The other half of PRD.md's Success — a person starting the service, POSTing a
real file with ``curl`` and opening the link in a browser — is a manual run, as
TASKS.md item 13 says; the cases here walk the same journey against the same
command, so the manual run has nothing left to discover.
"""

from __future__ import annotations

import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

import httpx

from app import config
from app.store import Store

# The repository root, which is the working directory ARCHITECTURE.md's
# Verification and README.md Run start the service from.
REPO_ROOT = Path(__file__).resolve().parents[1]

# The documented ASGI target, host and port (README.md Run, AGENTS.md "Install,
# run, test", ARCHITECTURE.md Verification).
ASGI_TARGET = "app.main:app"
HOST = "127.0.0.1"
DOCUMENTED_PORT = 8000

# The interpreter the documents spell as `python`. A case runs the same command
# with the interpreter running the suite, so no case depends on what `python`
# means on PATH.
DOCUMENTED_INTERPRETER = "python"

# The port a case asks uvicorn for: zero, so the operating system picks a free
# one and the case reads it from the process's own log. This is the only
# difference between the command run here and the documented one, and tc1305
# checks that.
EPHEMERAL_PORT = 0

# The paths the cases ask for (ARCHITECTURE.md Routes): the documented create
# and read path, and a path that reaches no route at all.
PASTES_PATH = "/pastes"
UNMATCHED_PATH = "/nothing-here"

# The content type of the answer to a read (ARCHITECTURE.md Read) and the body
# of the app's one 404 (ARCHITECTURE.md Error contract).
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
NOT_FOUND_BODY = b'{"error":"not_found"}'

# The headers that would tell a reader "this paste expired" rather than "this id
# never existed" if the 404 carried one (PRD.md item 5), and the header every
# real response carries afresh — so two answers written in different seconds are
# compared on everything else.
HINT_HEADERS = ("retry-after", "www-authenticate", "location")
VOLATILE_HEADERS = ("date",)

# What the process's log must not contain for a start or a stop to count as
# clean: a Python traceback, uvicorn's import failure, or an error line from
# uvicorn's logger.
FAILURE_MARKERS = ("Traceback", "Error loading ASGI app", "ERROR:")

# The line uvicorn logs once the lifespan's shutdown has finished, which for
# this app means the store's one connection has been closed (app/main.py,
# ``lifespan``). It is uvicorn's wording, and it is what separates a handled
# signal from a kill in the log.
UVICORN_SHUTDOWN_COMPLETE_LINE = "Application shutdown complete."

# How uvicorn ends on POSIX: the signal is handled, the application shuts down
# and then the signal is re-raised so the exit status reports it. A clean stop
# is therefore a plain zero or SIGTERM's negative code; a kill would report
# -SIGKILL instead, which is why the pair is asserted rather than accepted.
CLEAN_EXIT_CODES = (0, -signal.SIGTERM)

# The line uvicorn prints with the address it bound, which is how a case learns
# the port when it asked for port zero.
BOUND_ADDRESS_PATTERN = re.compile(rf"http://{re.escape(HOST)}:(\d+)")

# The ```verify block of ARCHITECTURE.md and the directives inside it: the
# command to start, the url to wait for, and one line per request to make.
VERIFY_BLOCK_PATTERN = re.compile(r"```verify\n(?P<body>.*?)```", re.DOTALL)
START_DIRECTIVE = "start"
READY_DIRECTIVE = "ready"
CHECK_DIRECTIVE = "check"
CHECK_PATTERN = re.compile(r"(?P<method>[A-Z]+)\s+(?P<url>\S+)\s+(?P<status>\d{3})")

# The documents this task measures the run path against (TASKS.md item 13).
ARCHITECTURE_FILENAME = "ARCHITECTURE.md"
README_FILENAME = "README.md"

# The install and test commands README.md gives the operator.
DOCUMENTED_INSTALL_COMMAND = "python -m pip install -r requirements-dev.txt"
DOCUMENTED_TEST_COMMAND = "python -m pytest"

# How long a case waits for the process to name its port and answer the
# readiness probe, for each request, and for a signal to be followed by the end
# of the shutdown. Generous, because they bound a failure rather than a
# successful run, which takes a fraction of a second on this machine.
STARTUP_TIMEOUT_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 20.0

# How often a case looks while it waits for the process to come up.
POLL_INTERVAL_SECONDS = 0.05

# The two files a case owns: the throwaway database the process is pointed at
# through PASTEBIN_DB, and the log it writes, both under the case's ``tmp_path``
# so the operator's ``pastebin.db`` is never opened (PRD.md Success).
DB_FILENAME = "run-path-pastes.db"
LOG_FILENAME = "uvicorn.log"

# PRD.md Success's latency default for a paste at the 1 MiB ceiling. It is
# "intended as 'no obvious stall', not a benchmark", so the case watches the
# documented budget rather than a tight one: the round trip measured on this
# machine is around 7 ms, two orders of magnitude inside it.
LATENCY_BUDGET_SECONDS = 0.2

# The ids and the texts the seeded case uses: one paste an hour past its
# deadline and one with its full three hours ahead, both written into the
# throwaway file before the process opens it (PRD.md items 4, 5 and 6).
EXPIRED_ID = "E" * 22
LIVE_ID = "L" * 22
UNKNOWN_ID = "Z" * 22
MALFORMED_ID = "no-such-id"
EXPIRED_MARGIN_SECONDS = 3600.0
EXPIRED_TEXT = "the paste whose deadline has already passed\né ☃ 😀\n"
LIVE_TEXT = "the paste that is still inside its three hours 😀\n"

# The text the manual end-to-end check is walked with, carrying the shapes
# PRD.md item 2 names — newlines, leading and trailing whitespace, non-ASCII
# characters and emoji — so an answer from the running process that trimmed,
# re-encoded or rendered the paste could not come back byte for byte.
END_TO_END_TEXT = (
    "  a stack trace a teammate will open  \n"
    "Traceback (most recent call last):\n"
    '\tFile "app.py", line 42, in <module>\n'
    "ValueError: é ☃ 😀 — the text must survive byte for byte\r\n"
    "  trailing whitespace kept  \n"
)

# The body tc1304 measures: exactly config.MAX_PASTE_BYTES of ASCII, bracketed
# by a first and a last line so a truncated or padded answer is visible.
CEILING_HEAD = b"first line of the largest paste the service accepts\n"
CEILING_TAIL = b"\nlast line of the largest paste the service accepts\n"

# A small paste sent before the ceiling is timed, so the measurement does not
# carry the first request's own cost.
WARM_UP_BODY = b"a small paste to warm the process up\n"


def _document_text(filename: str) -> str:
    """One of the repository's documents, as written."""
    return (REPO_ROOT / filename).read_text(encoding="utf-8")


def _flattened(text: str) -> str:
    """``text`` with every run of whitespace collapsed to one space.

    The documents wrap their command lines for width, so the assertions that a
    document contains a command ask after this normalisation: the tokens have to
    be there in order, but the line breaks are the document's business.
    """
    return " ".join(text.split())


def _start_command(port: int) -> list[str]:
    """The documented start command's words, on ``port``.

    README.md Run, AGENTS.md and ARCHITECTURE.md's verify block all start the
    service the same way; this builds that argv with the interpreter running the
    suite in place of the name ``python``, so a case exercises the documented
    command rather than an approximation of it (TASKS.md item 13).
    """
    return [
        sys.executable,
        "-m",
        "uvicorn",
        ASGI_TARGET,
        "--host",
        HOST,
        "--port",
        str(port),
    ]


def _documented_start_command() -> str:
    """The documented start command as the documents write it.

    The interpreter is spelled ``python`` here because that is what the
    documents say; everything after it is the argv ``_start_command`` builds, so
    tc1305 can compare the two word for word.
    """
    return " ".join([DOCUMENTED_INTERPRETER, *_start_command(DOCUMENTED_PORT)[1:]])


def _documented_origin() -> str:
    """The origin the documents' urls are written against."""
    return f"http://{HOST}:{DOCUMENTED_PORT}"


def _verify_block() -> str:
    """The body of ARCHITECTURE.md's ```verify block, the platform's gate.

    The block is the documented contract between the repository and the platform
    that runs it: a command to start, a url to wait for, and one request per line
    to check. Reading it here rather than repeating it is what makes the case
    below a check of the gate rather than a copy of it.
    """
    match = VERIFY_BLOCK_PATTERN.search(_document_text(ARCHITECTURE_FILENAME))
    assert match is not None, (
        f"{ARCHITECTURE_FILENAME} must hold the ```verify block the platform "
        "runs before each pull request"
    )
    return match.group("body")


def _verify_directive(name: str) -> str:
    """The value of one ``name: value`` line of the verify block."""
    for line in _verify_block().splitlines():
        directive, separator, value = line.strip().partition(":")
        if separator and directive == name:
            return value.strip()
    raise AssertionError(f"the verify block has no `{name}:` line")


def _verify_checks() -> list[tuple[str, str, int]]:
    """The verify block's ``check`` lines as ``(method, url, status)``."""
    checks = []
    for line in _verify_block().splitlines():
        directive, separator, value = line.strip().partition(":")
        if not separator or directive != CHECK_DIRECTIVE:
            continue
        match = CHECK_PATTERN.fullmatch(value.strip())
        assert match is not None, f"unreadable check line in the verify block: {line!r}"
        checks.append(
            (match.group("method"), match.group("url"), int(match.group("status")))
        )
    return checks


def _ready_path() -> str:
    """The path of the url the verify block waits for before checking."""
    ready_url = _verify_directive(READY_DIRECTIVE)
    assert ready_url.startswith(_documented_origin()), (
        f"the verify block's ready url must be on {_documented_origin()}, "
        f"found {ready_url}"
    )
    return ready_url[len(_documented_origin()) :]


def _on_the_running_service(url: str, base_url: str) -> str:
    """A documented url, moved onto the port the case's process was given.

    The verify block's urls carry the documented host and port; the process
    under test listens on an ephemeral one. Only the origin is replaced: the
    path and the id are the block's own.
    """
    assert url.startswith(_documented_origin()), (
        f"a verify block url must be on {_documented_origin()}, found {url}"
    )
    return base_url + url[len(_documented_origin()) :]


def _db_path(tmp_path: Path) -> Path:
    """The throwaway database a case's process is pointed at through PASTEBIN_DB."""
    return tmp_path / DB_FILENAME


def _ceiling_body() -> bytes:
    """A body of exactly ``config.MAX_PASTE_BYTES`` bytes."""
    filler = config.MAX_PASTE_BYTES - len(CEILING_HEAD) - len(CEILING_TAIL)
    return CEILING_HEAD + b"x" * filler + CEILING_TAIL


def _comparable_headers(response: httpx.Response) -> dict[str, str]:
    """A response's headers without the one every real answer writes afresh."""
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in VOLATILE_HEADERS
    }


def _seed_pastes(db_path: Path) -> None:
    """Write one dead paste and one live one into the throwaway file.

    The process started by the documented command uses the real clock, so a case
    cannot move time under it: the deadline to cross is therefore written into
    the file before the process opens it, an hour behind the real instant for the
    dead paste and a full three hours ahead for the live one. Both rows are the
    shape the create route writes — the deadline is the creation instant plus the
    fixed lifetime — so the running service sees two pastes that differ only in
    which side of the real instant their deadline falls on (PRD.md items 4, 5
    and 6).
    """
    now = time.time()
    expired_deadline = now - EXPIRED_MARGIN_SECONDS
    store = Store(db_path)
    try:
        store.insert(
            EXPIRED_ID,
            EXPIRED_TEXT,
            expired_deadline - config.PASTE_TTL_SECONDS,
            expired_deadline,
        )
        store.insert(LIVE_ID, LIVE_TEXT, now, now + config.PASTE_TTL_SECONDS)
    finally:
        store.close()


class RunningService:
    """One child process of the documented start command, and how to talk to it.

    The process is started on a throwaway database and an ephemeral port, and it
    is left running until ``stop()`` asks it to end the way an operator would:
    the signal uvicorn handles, then a wait for the shutdown the lifespan runs.
    A process that will not stop is killed, so a failing case cannot leave one
    behind. One ``httpx`` client is kept open for the life of the service, so a
    request measures the service rather than a fresh TCP connection.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        log_path: Path,
        log_file: BinaryIO,
        db_path: Path,
    ) -> None:
        self.process = process
        self.log_path = log_path
        self.db_path = db_path
        self.base_url = ""
        self._log_file = log_file
        self._client: httpx.Client | None = None

    def wait_until_ready(self, ready_path: str) -> None:
        """Wait for the bound port and for the readiness probe to answer 200.

        The port is read from the process's own log — that is what port zero
        means — and ``ready_path`` is the one the verify block names, so the wait
        is the gate's own readiness rule. A process that fails to import the
        target, or cannot bind, exits without ever answering, and its log is what
        the failure reports (TASKS.md item 13: the command must start clean).
        """
        port = self._wait_for_port()
        self.base_url = f"http://{HOST}:{port}"
        self._client = httpx.Client(
            base_url=self.base_url, timeout=REQUEST_TIMEOUT_SECONDS
        )

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                if self._client.get(ready_path).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(POLL_INTERVAL_SECONDS)

        raise AssertionError(
            f"the documented start command did not answer {ready_path} on "
            f"{self.base_url or '<no address>'}; exit code "
            f"{self.process.poll()}; log:\n{self.log()}"
        )

    def _wait_for_port(self) -> int:
        """The port uvicorn reports it bound, or a failure carrying the log."""
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            match = BOUND_ADDRESS_PATTERN.search(self.log())
            if match is not None:
                return int(match.group(1))
            if self.process.poll() is not None:
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        raise AssertionError(
            "the documented start command never reported an address; exit code "
            f"{self.process.poll()}; log:\n{self.log()}"
        )

    def request(self, method: str, url: str) -> httpx.Response:
        """One request to the running service, url absolute or repo-relative."""
        assert self._client is not None, "the service was already stopped"
        return self._client.request(method, url)

    def get(self, url: str) -> httpx.Response:
        """A GET to the running service."""
        return self.request("GET", url)

    def post(self, url: str, content: bytes) -> httpx.Response:
        """A POST to the running service, with the text as the raw body.

        The body is sent as bytes and nothing else, exactly as
        ``curl --data-binary @file`` sends it (ARCHITECTURE.md's decision row
        "Create body is raw text", PRD.md Success's manual check).
        """
        assert self._client is not None, "the service was already stopped"
        return self._client.post(url, content=content)

    def log(self) -> str:
        """Everything the process has written so far, stdout and stderr together."""
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def rows(self) -> list[tuple[str, str, float, float]]:
        """The pastes in the throwaway file, as ``(id, text, created_at, expires_at)``.

        Read through a second connection to the file the process was pointed at,
        so "the running service stored this" and "the process reclaimed that" are
        measured on the database rather than on an answer (PRD.md items 4, 6 and
        8). WAL mode lets this read while the service is running.
        """
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                "SELECT id, text, created_at, expires_at FROM pastes"
            ).fetchall()
        finally:
            connection.close()

    def stop(self) -> None:
        """End the process the way an operator does, then read its exit status.

        SIGTERM through ``terminate()``, then a wait: uvicorn handles the signal,
        runs the lifespan's shutdown — which closes the store's one connection —
        and ends. A process that does not end inside the timeout is killed, so a
        failing case leaves nothing running. Calling this twice, or for a process
        that already ended by itself, does nothing further.
        """
        if self._client is not None:
            self._client.close()
            self._client = None

        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

        if not self._log_file.closed:
            self._log_file.close()

    @property
    def exit_code(self) -> int:
        """The process's exit status, which is only meaningful once it has stopped."""
        assert self.process.poll() is not None, "the service is still running"
        return int(self.process.returncode)


def _start_service(tmp_path: Path) -> RunningService:
    """Start the documented command as one process on a throwaway database.

    ``PASTEBIN_DB`` is set for the child only, so the process opens the case's
    file and never the operator's ``pastebin.db`` (PRD.md Success). ``PYTHONPATH``
    is dropped so the target has to be importable from the repository root by the
    documented install rather than by whatever the suite's own process has on its
    path, which is the claim ARCHITECTURE.md's Verification makes.
    """
    db_path = _db_path(tmp_path)
    log_path = tmp_path / LOG_FILENAME
    log_file = log_path.open("wb")

    env = dict(os.environ)
    env[config.DB_PATH_ENV_VAR] = str(db_path)
    env.pop("PYTHONPATH", None)

    try:
        process = subprocess.Popen(
            _start_command(EPHEMERAL_PORT),
            cwd=REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        log_file.close()
        raise

    service = RunningService(
        process=process, log_path=log_path, log_file=log_file, db_path=db_path
    )
    try:
        service.wait_until_ready(_ready_path())
    except BaseException:
        # A start that never became ready stops here rather than leaving a child
        # process behind for the rest of the suite (which is also what makes the
        # failure report readable: the log is complete and the port is free).
        service.stop()
        raise
    return service


@contextmanager
def _running_service(tmp_path: Path) -> Iterator[RunningService]:
    """The documented command, running for the length of one case.

    Stopped in a ``finally`` so a failing case cannot leave a process behind, and
    the stopped service is still readable afterwards: ``log()``, ``rows()`` and
    ``exit_code`` are how the cases below measure a clean start and a clean stop.
    """
    service = _start_service(tmp_path)
    try:
        yield service
    finally:
        service.stop()


def test_tc1301_the_documented_start_command_starts_cleanly_and_the_verify_block_passes(
    tmp_path: Path,
) -> None:
    """TASKS.md item 13: one command, a clean start, and ARCHITECTURE.md's gate.

    The block is read from the document rather than repeated, so every request
    the platform would make is made here against the same command the platform
    starts — and the statuses it documents are the statuses answered.
    """
    ready_path = _ready_path()
    checks = _verify_checks()
    assert checks, "the verify block must hold at least one check line"

    with _running_service(tmp_path) as service:
        # One process, up and serving while the checks run (PRD.md Success:
        # "starts with one command as one process").
        assert service.process.poll() is None
        assert service.base_url.startswith(f"http://{HOST}:")

        # The start was clean: no traceback, no import failure, no error line.
        log = service.log()
        for marker in FAILURE_MARKERS:
            assert marker not in log, f"the run path logged {marker!r}:\n{log}"

        # The readiness probe the block names answers 200, which is what the wait
        # above already required; the probe is repeated so the case reads as the
        # gate does (ARCHITECTURE.md Verification).
        probe = service.get(ready_path)
        assert probe.status_code == 200
        assert probe.json() == {"status": "ok"}

        # Every check line, replayed on the port this process bound.
        for method, url, expected_status in checks:
            response = service.request(
                method, _on_the_running_service(url, service.base_url)
            )
            assert response.status_code == expected_status, (
                f"{method} {url} answered {response.status_code}, "
                f"the verify block documents {expected_status}"
            )

        # The 404 checks are the app's one 404: the same body, and the same
        # headers bar the instant the answer was written (PRD.md item 5).
        not_found = [
            service.request(method, _on_the_running_service(url, service.base_url))
            for method, url, expected_status in checks
            if expected_status == 404
        ]
        assert not_found, "the verify block must check at least one 404"
        for response in not_found:
            assert response.content == NOT_FOUND_BODY
            assert _comparable_headers(response) == _comparable_headers(not_found[0])

        # The process opened the case's throwaway file and nothing else: the
        # store is created at startup, and PASTEBIN_DB is what told it where
        # (ARCHITECTURE.md Data, PRD.md Success).
        assert service.db_path.is_file()
        assert service.rows() == []


def test_tc1302_the_manual_end_to_end_check_against_the_running_process(
    tmp_path: Path,
) -> None:
    """TASKS.md item 13's manual check, walked by a case (PRD.md items 1, 2 and 4).

    A POST carrying text answers with a link, the link is opened exactly as it
    was returned, and the text comes back byte for byte — with the row in the
    throwaway file carrying the instant the create route used and the fixed three
    hours on top of it, which is the deadline the running process enforces.
    """
    with _running_service(tmp_path) as service:
        created = service.post(PASTES_PATH, END_TO_END_TEXT.encode("utf-8"))

        assert created.status_code == 201, created.text
        paste = created.json()
        assert set(paste) == {"id", "url", "expires_at"}
        # The link is absolute and built from the host the caller reached: this
        # process's own origin, so it can be pasted into a browser as-is.
        assert paste["url"].startswith(f"{service.base_url}{PASTES_PATH}/")

        # The reader opens the url the answer returned, not a path rebuilt from
        # the id, and sees the text exactly as it was posted.
        served = service.get(paste["url"])

        assert served.status_code == 200
        assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert served.content == END_TO_END_TEXT.encode("utf-8")
        assert "content-disposition" not in served.headers

        # Stored once, by the running process, in the throwaway file, with the
        # deadline the fixed lifetime produces (PRD.md item 4).
        ((paste_id, text, created_at, expires_at),) = service.rows()
        assert paste_id == paste["id"]
        assert text == END_TO_END_TEXT
        assert expires_at - created_at == config.PASTE_TTL_SECONDS
        # The creation instant is a real one, and the paste is inside its three
        # hours at the moment a case looks at it.
        assert created_at <= time.time() < expires_at


def test_tc1303_the_running_process_answers_one_404_for_every_kind_of_miss(
    tmp_path: Path,
) -> None:
    """PRD.md items 5 and 6: identical answers, and the dead paste's row reclaimed.

    The dead paste is seeded into the throwaway file with a deadline an hour
    behind, because the real clock of a real process cannot be moved; the live
    one is seeded beside it as the control, so a service that answered 404 for
    everything would fail here.
    """
    _seed_pastes(_db_path(tmp_path))

    with _running_service(tmp_path) as service:
        misses = {
            "expired paste": service.get(f"{PASTES_PATH}/{EXPIRED_ID}"),
            "made-up id": service.get(f"{PASTES_PATH}/{UNKNOWN_ID}"),
            "malformed id": service.get(f"{PASTES_PATH}/{MALFORMED_ID}"),
            "unmatched path": service.get(UNMATCHED_PATH),
        }

        reference = misses["made-up id"]
        assert reference.status_code == 404
        assert reference.content == NOT_FOUND_BODY
        assert reference.json() == {"error": "not_found"}

        # One status, one body, one set of headers, whichever of the four it is
        # (PRD.md item 5).
        for case, response in misses.items():
            assert response.status_code == reference.status_code, case
            assert response.content == reference.content, case
            assert _comparable_headers(response) == _comparable_headers(reference), case
            for header in HINT_HEADERS:
                assert header not in response.headers, (case, header)

        # Nothing of the dead paste is in the answer, wherever it was asked for.
        for response in misses.values():
            assert EXPIRED_TEXT.encode("utf-8") not in response.content
            assert EXPIRED_ID not in response.text

        # The read reclaimed the dead row rather than hiding it, and left the
        # paste inside its three hours alone, with the deadline it has left
        # (PRD.md items 4 and 6).
        (remaining,) = service.rows()
        assert remaining[0] == LIVE_ID
        assert remaining[1] == LIVE_TEXT
        assert remaining[3] - remaining[2] == config.PASTE_TTL_SECONDS
        assert remaining[2] <= time.time() < remaining[3]

        live = service.get(f"{PASTES_PATH}/{LIVE_ID}")
        assert live.status_code == 200
        assert live.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert live.content == LIVE_TEXT.encode("utf-8")


def test_tc1304_a_paste_at_the_ceiling_is_answered_inside_the_documented_budget(
    tmp_path: Path,
) -> None:
    """PRD.md Success: a 1 MiB paste comes back in under 200 ms, "not a benchmark".

    Both halves are timed: the create, which reads the body and commits the row,
    and the read, which writes the stored text back. A small paste is sent first
    so the measurement carries neither the first request's cost nor a fresh TCP
    connection's, and the body is checked byte for byte besides, so the budget is
    spent on a paste that really made the round trip.
    """
    body = _ceiling_body()
    assert len(body) == config.MAX_PASTE_BYTES

    with _running_service(tmp_path) as service:
        warm_up = service.post(PASTES_PATH, WARM_UP_BODY)
        assert warm_up.status_code == 201
        assert service.get(warm_up.json()["url"]).status_code == 200

        started = time.perf_counter()
        created = service.post(PASTES_PATH, body)
        create_seconds = time.perf_counter() - started

        assert created.status_code == 201, created.text
        paste = created.json()

        started = time.perf_counter()
        served = service.get(paste["url"])
        read_seconds = time.perf_counter() - started

        assert served.status_code == 200
        assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert served.content == body
        assert len(served.content) == config.MAX_PASTE_BYTES

        # The paste really is stored at the ceiling: what came back was the row
        # the running process wrote, not a body it happened to hold in memory.
        stored = {row[0]: row[1] for row in service.rows()}
        assert len(stored[paste["id"]].encode("utf-8")) == config.MAX_PASTE_BYTES

        assert create_seconds < LATENCY_BUDGET_SECONDS, (
            f"a paste at the {config.MAX_PASTE_BYTES}-byte ceiling took "
            f"{create_seconds:.3f}s to store, the documented budget is "
            f"{LATENCY_BUDGET_SECONDS}s"
        )
        assert read_seconds < LATENCY_BUDGET_SECONDS, (
            f"a paste at the {config.MAX_PASTE_BYTES}-byte ceiling took "
            f"{read_seconds:.3f}s to come back, the documented budget is "
            f"{LATENCY_BUDGET_SECONDS}s"
        )


def test_tc1305_the_documented_commands_are_the_commands_this_module_runs() -> None:
    """TASKS.md item 13: the README commands and the verify block match the code.

    The block's ``start`` line, README.md's run command and the argv this module
    builds have to be the same words on the same host, port and target — checked
    word for word, so the run path the cases exercise is the one the documents
    tell the operator to run. The install and test commands, and the database
    variable and default filename README.md names, are checked against the module
    that reads them.
    """
    architecture = _document_text(ARCHITECTURE_FILENAME)
    readme = _document_text(README_FILENAME)
    documented = _documented_start_command()

    # The gate's start line, README's run command, and this module's argv.
    assert _verify_directive(START_DIRECTIVE) == documented
    assert documented in _flattened(readme)
    assert documented in _flattened(architecture)
    assert " ".join(_start_command(DOCUMENTED_PORT)[1:]) == " ".join(
        documented.split()[1:]
    )

    # A case runs the same command on a port of its own choosing, and the port is
    # the only word that differs.
    assert _start_command(EPHEMERAL_PORT)[:-1] == _start_command(DOCUMENTED_PORT)[:-1]
    assert _start_command(EPHEMERAL_PORT)[-1] == str(EPHEMERAL_PORT)

    # The commands the operator is given for installing and for testing.
    assert DOCUMENTED_INSTALL_COMMAND in _flattened(readme)
    assert DOCUMENTED_TEST_COMMAND in _flattened(readme)

    # The database README describes is the one the code reads.
    assert config.DB_PATH_ENV_VAR in readme
    assert config.DEFAULT_DB_FILENAME in readme

    # The readiness probe and the checks the gate runs are readable, so the case
    # above is a check of the block rather than of an empty parse.
    assert _ready_path().startswith("/")
    assert _verify_checks()
    for method, url, status in _verify_checks():
        assert method and url.startswith(_documented_origin())
        assert 100 <= status < 600


def test_tc1306_the_operator_ending_the_process_is_a_clean_shutdown(
    tmp_path: Path,
) -> None:
    """PRD.md item 8 on the real process: a stopped service leaves its work behind.

    The signal uvicorn handles is sent, the shutdown it runs is observed in the
    log, the exit status is the signal rather than a kill's, and the paste
    created through the process is still in the throwaway file afterwards — so a
    paste inside its three hours survives a stop and a start for reasons the
    suite can see on the file rather than take on trust.
    """
    with _running_service(tmp_path) as service:
        created = service.post(PASTES_PATH, END_TO_END_TEXT.encode("utf-8"))
        assert created.status_code == 201, created.text
        paste = created.json()
        assert service.process.poll() is None

    # Outside the ``with``: the process has been asked to end, with the signal an
    # operator's Ctrl-C or service manager sends.
    log = service.log()
    assert service.exit_code in CLEAN_EXIT_CODES, (
        f"the process ended with {service.exit_code}, a clean stop is "
        f"{CLEAN_EXIT_CODES}"
    )
    assert UVICORN_SHUTDOWN_COMPLETE_LINE in log, (
        f"the lifespan's shutdown did not finish; log:\n{log}"
    )
    for marker in FAILURE_MARKERS:
        assert marker not in log, f"the stop logged {marker!r}:\n{log}"

    # The store was closed cleanly, so the paste it accepted is in the file: a
    # process started on that file again would serve it (PRD.md item 8).
    ((paste_id, text, created_at, expires_at),) = service.rows()
    assert paste_id == paste["id"]
    assert text == END_TO_END_TEXT
    assert expires_at - created_at == config.PASTE_TTL_SECONDS
