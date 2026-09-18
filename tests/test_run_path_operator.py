"""Task 13 continued: the run path as the operator meets it (TASKS.md item 13).

TASKS.md item 13 wires the run path: ``python -m uvicorn app.main:app --host
127.0.0.1 --port 8000`` starts clean, the ARCHITECTURE.md verify block passes,
the README commands match, and a 1 MiB paste answers in under 200 ms on this
machine. Its success criterion is the operator's half of PRD.md's Success — "the
service starts with one command as one process; unexpired pastes still resolve
after a restart" — together with the manual end-to-end check and the latency
default.

``tests/test_run_path.py`` (same task, committed beside this module) starts the
documented command as a child process and pins the clean start, the verify
block, the README commands and the 1 MiB budget. What it does not cover are the
two acceptance cases below, which need a process of their own and are the
operator's own side of the run path:

- tc1307: the command needs no setting at all. Started in a directory the case
  made, with ``PASTEBIN_DB`` unset, it answers the readiness probe, serves the
  create → link → read journey, and keeps its one database file under the
  documented default name in the working directory it was started from — the
  "no account and no setup" start of README.md Run, ARCHITECTURE.md Data's
  "default ``pastebin.db`` in the working directory" and PRD.md Users' service
  operator (PRD.md Success: one command as one process).
- tc1308: the paste created through one run of the documented command is served
  again by a second run of it over the same database file, with the deadline the
  first run stored — the operator stopping and starting the service, which is
  PRD.md Success's "unexpired pastes still resolve after a restart", PRD.md item
  8, and PRD.md item 4's "service downtime or a restart does not extend it".

Why each case starts a process rather than using the suite's in-process client:

- **It is a property of the run path.** Every other module drives the real
  ``app.main:app`` through FastAPI's test client inside the suite's own
  interpreter (tests/conftest.py), which is what keeps the suite one command
  with nothing to start first and no network (AGENTS.md Conventions, PRD.md
  Success). The child process here is the documented command itself, so what it
  shows — the target importable after the documented install, the default
  database resolved against its working directory, a fresh interpreter reading
  the file an earlier one wrote — is nothing an in-process client can show.
- **The real clock.** The documented command installs no clock override, so a
  process started this way works in real instants. Neither case crosses the
  three-hour deadline: the expiry boundary belongs to the injected-clock cases
  (TASKS.md items 8, 9 and 12, tests/test_expiry_boundary.py, tests/test_reclaim.py
  and tests/test_restart.py), which is why nothing here waits and both cases
  stay well inside a paste's lifetime.
- **An ephemeral port.** The documented command names port 8000; both cases run
  the same target and host with ``--port 0`` and read the port uvicorn reports,
  because a fixed port belongs to the machine rather than to the code, so a
  suite holding one could fail on any host where 8000 is taken
  (tests/test_run_path.py, tc1305, pins that the port is the only word that
  differs from the documented command).
- **A throwaway database, always.** ``PASTEBIN_DB`` points each case at a file
  under its ``tmp_path`` — or, in tc1307, at no variable at all and a working
  directory of the case's own — so the operator's ``pastebin.db`` is never
  opened (PRD.md Success).

The restart here is across two processes, not across two builds inside one:
``tests/test_restart.py`` already covers the service stopped and started inside
one interpreter, which is what lets a case move the clock across the downtime
without waiting; what this module adds is the same claim on the wire, with a
fresh process reopening the file (tc1308), and the default configuration a
first start actually runs with (tc1307).
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

# The repository root, which is the working directory the documents start the
# service from (ARCHITECTURE.md Verification, README.md Run).
REPO_ROOT = Path(__file__).resolve().parents[1]

# The documented ASGI target and host (README.md Run, AGENTS.md "Install, run,
# test").
ASGI_TARGET = "app.main:app"
HOST = "127.0.0.1"

# The port a case asks uvicorn for: zero, so the operating system picks a free
# one and the case reads it from the process's own log. A case's process must
# not depend on the machine's port 8000 being free (see the module docstring),
# and tests/test_run_path.py's tc1305 pins that the port is the only word that
# differs from the documented command.
EPHEMERAL_PORT = 0

# The documented paths (ARCHITECTURE.md Routes), the payload the readiness probe
# answers with (tests/test_health.py) and the content type of a served paste
# (ARCHITECTURE.md Read).
HEALTH_PATH = "/health"
HEALTH_PAYLOAD = {"status": "ok"}
PASTES_PATH = "/pastes"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"

# The database file README.md names as the default and ARCHITECTURE.md Data
# places "in the working directory": what a start with no ``PASTEBIN_DB`` opens,
# in the directory it was started from.
DOCUMENTED_DEFAULT_DB_FILENAME = "pastebin.db"

# The lifetime every paste gets, three hours from creation (PRD.md item 4).
THREE_HOURS_SECONDS = 3 * 60 * 60

# The line uvicorn prints with the address it bound, which is how a case learns
# the port when it asked for port zero.
BOUND_ADDRESS_PATTERN = re.compile(rf"http://{re.escape(HOST)}:(\d+)")

# What the process's log must not contain for a start or a stop to count as
# clean: a Python traceback, uvicorn's import failure, or an error line from
# uvicorn's logger.
FAILURE_MARKERS = ("Traceback", "Error loading ASGI app", "ERROR:")

# How uvicorn ends on POSIX: the signal is handled, the application shuts down
# and then the signal is re-raised so the exit status reports it. A stop is
# therefore a plain zero or SIGTERM's negative code.
CLEAN_EXIT_CODES = (0, -signal.SIGTERM)

# How long a case waits for the process to name its port and answer the
# readiness probe, for each request, and for a signalled process to end.
# Generous, because they bound a failure rather than a successful run, which
# takes a fraction of a second on this machine.
STARTUP_TIMEOUT_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 20.0

# How often a case looks while it waits for the process to come up.
POLL_INTERVAL_SECONDS = 0.05

# The two files a case owns: the log the process writes, and the throwaway
# database tc1308 points the process at through ``PASTEBIN_DB`` (tc1307 points
# nothing and lets the process use the documented default name instead). Both
# live under the case's ``tmp_path``.
LOG_FILENAME = "uvicorn.log"
THROWAWAY_DB_FILENAME = "run-path-operator-pastes.db"

# The text both cases walk the journey with: PRD.md item 2's shapes — newlines,
# leading and trailing whitespace, non-ASCII characters and emoji — so an answer
# that trimmed, re-encoded or rendered the paste could not come back byte for
# byte, and realistic enough to be the stack trace PRD.md's Goal describes.
OPERATOR_TEXT = (
    "  a stack trace the operator will hand on  \n"
    "Traceback (most recent call last):\n"
    '\tFile "app.py", line 42, in <module>\n'
    "ValueError: é ☃ 😀 — the text must survive a restart\r\n"
    "  trailing whitespace kept  \n"
)


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


def _environment(database_path: str | None) -> dict[str, str]:
    """The environment the documented command is started with.

    ``PASTEBIN_DB`` is either the case's throwaway file or absent, which is the
    operator's start with no setup at all (README.md Run: "no account and no
    setup"). ``PYTHONPATH`` is dropped either way, so the target has to be
    importable because ``python -m pip install -r requirements-dev.txt``
    installed it, which is what the documents tell the operator to do
    (ARCHITECTURE.md Verification).
    """
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    if database_path is None:
        environment.pop(config.DB_PATH_ENV_VAR, None)
    else:
        environment[config.DB_PATH_ENV_VAR] = database_path
    return environment


class OperatorService:
    """One run of the documented start command, and how to talk to it.

    The process is started on the case's database and an ephemeral port, and it
    is left running until ``stop()`` ends it the way an operator does: the
    signal uvicorn handles, then a wait for the shutdown the lifespan runs. A
    process that will not stop is killed, so a failing case cannot leave one
    behind. One ``httpx`` client is kept open for the life of the run, so a
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

    def wait_until_ready(self) -> None:
        """Wait for the bound port and for the readiness probe to answer 200.

        The port is read from the process's own log — that is what port zero
        means — and the probe is ``GET /health``, the endpoint AGENTS.md says
        "answers as soon as the process is up". A process that fails to import
        the target, or cannot bind, exits without ever answering, and its log is
        what the failure reports (TASKS.md item 13: the command must start
        clean).
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
                if self._client.get(HEALTH_PATH).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(POLL_INTERVAL_SECONDS)

        raise AssertionError(
            f"the documented start command did not answer {HEALTH_PATH} on "
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

    def get(self, url: str) -> httpx.Response:
        """A GET to the running service, url absolute or repo-relative."""
        assert self._client is not None, "the service was already stopped"
        return self._client.get(url)

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
        """The pastes in the case's file, as ``(id, text, created_at, expires_at)``.

        Read through a second connection to the file the process opened, so "the
        run stored this" and "the next run finds the same row" are measured on
        the database rather than on an answer (PRD.md items 4 and 8). WAL mode
        lets this read while the service is running.
        """
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                "SELECT id, text, created_at, expires_at FROM pastes"
            ).fetchall()
        finally:
            connection.close()

    def stop(self) -> None:
        """End the process the way an operator does, then keep its exit status.

        SIGTERM through ``terminate()``, then a wait: uvicorn handles the signal,
        runs the lifespan's shutdown — which closes the store's one connection —
        and ends. A process that does not end inside the timeout is killed, so a
        failing case leaves nothing running.
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


def _start(
    working_dir: Path,
    db_path: Path,
    log_path: Path,
    database_path: str | None,
) -> OperatorService:
    """Start the documented command as one process and wait for it to be ready.

    ``working_dir`` is the directory the process is started from, which is what
    decides where the default database goes when ``database_path`` is ``None``;
    ``db_path`` is the file the case expects that to be. A start that never
    becomes ready is stopped here rather than left behind for the rest of the
    suite, and the failure carries the log.
    """
    log_file = log_path.open("wb")

    try:
        process = subprocess.Popen(
            _start_command(EPHEMERAL_PORT),
            cwd=working_dir,
            env=_environment(database_path),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        log_file.close()
        raise

    service = OperatorService(
        process=process, log_path=log_path, log_file=log_file, db_path=db_path
    )
    try:
        service.wait_until_ready()
    except BaseException:
        service.stop()
        raise
    return service


@contextmanager
def _running_service(tmp_path: Path) -> Iterator[OperatorService]:
    """The documented command, on the case's throwaway database, from the repo root.

    ``PASTEBIN_DB`` is set for the child only, so the process opens the case's
    file and never the operator's ``pastebin.db`` (PRD.md Success). Stopped in a
    ``finally`` so a failing case cannot leave a process behind; the stopped
    service is still readable afterwards through ``log()``, ``rows()`` and
    ``exit_code``.
    """
    db_path = tmp_path / THROWAWAY_DB_FILENAME
    service = _start(
        working_dir=REPO_ROOT,
        db_path=db_path,
        log_path=tmp_path / LOG_FILENAME,
        database_path=str(db_path),
    )
    try:
        yield service
    finally:
        service.stop()


@contextmanager
def _running_service_with_no_configuration(
    working_dir: Path,
) -> Iterator[OperatorService]:
    """The documented command with nothing configured at all.

    ``PASTEBIN_DB`` is absent from the child's environment and its working
    directory is ``working_dir``, so the file it opens is the documented default
    resolved against that directory (README.md Run, ARCHITECTURE.md Data). The
    case's expectation of where that file lands is passed in as ``db_path`` and
    asserted by the case itself.
    """
    db_path = working_dir / DOCUMENTED_DEFAULT_DB_FILENAME
    service = _start(
        working_dir=working_dir,
        db_path=db_path,
        log_path=working_dir / LOG_FILENAME,
        database_path=None,
    )
    try:
        yield service
    finally:
        service.stop()


def test_tc1307_the_documented_command_needs_no_configuration_and_keeps_its_database_in_the_working_directory(
    tmp_path: Path,
) -> None:
    """TASKS.md item 13's one-command start, with nothing set up (PRD.md Success).

    The service is started from a directory the case made, with ``PASTEBIN_DB``
    unset, and has to be a working service: it answers the readiness probe,
    stores the posted text and serves it back byte-for-byte. Its one database
    file is then the documented default, in the directory the process was
    started from — which is what "no setup" means for the operator (README.md
    Run, ARCHITECTURE.md Data, PRD.md Users).
    """
    working_dir = tmp_path / "operator-working-directory"
    working_dir.mkdir()

    with _running_service_with_no_configuration(working_dir) as service:
        # The process is up, and its start was clean: no traceback, no import
        # failure, no error line (TASKS.md item 13).
        for marker in FAILURE_MARKERS:
            assert marker not in service.log(), (
                f"the run path logged {marker!r}:\n{service.log()}"
            )

        probe = service.get(HEALTH_PATH)
        assert probe.status_code == 200
        assert probe.json() == HEALTH_PAYLOAD

        # The create journey works against a service started this way, and the
        # link the answer carries is absolute and on this process's own origin
        # (PRD.md items 1 and 4, README.md Run).
        created = service.post(PASTES_PATH, OPERATOR_TEXT.encode("utf-8"))

        assert created.status_code == 201, created.text
        paste = created.json()
        assert set(paste) == {"id", "url", "expires_at"}
        assert paste["url"] == f"{service.base_url}{PASTES_PATH}/{paste['id']}"

        served = service.get(paste["url"])

        assert served.status_code == 200
        assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert served.content == OPERATOR_TEXT.encode("utf-8")

        # Nothing was configured and nothing was set up: the one database file
        # is the documented default name in the working directory the command
        # was started from, and the paste the service accepted is in it with the
        # fixed three hours on the row (README.md Run, ARCHITECTURE.md Data,
        # PRD.md item 4).
        assert service.db_path.is_file()
        assert service.db_path.parent == working_dir
        assert service.db_path.name == DOCUMENTED_DEFAULT_DB_FILENAME

        ((paste_id, text, created_at, expires_at),) = service.rows()
        assert paste_id == paste["id"]
        assert text == OPERATOR_TEXT
        assert expires_at - created_at == THREE_HOURS_SECONDS
        assert created_at <= time.time() < expires_at

    # The operator's stop is clean, and the default database is left behind for
    # the next start to read rather than held in the process (PRD.md item 8).
    assert service.exit_code in CLEAN_EXIT_CODES
    for marker in FAILURE_MARKERS:
        assert marker not in service.log(), f"the stop logged {marker!r}"
    assert service.db_path.is_file()
    assert len(service.rows()) == 1


def test_tc1308_the_documented_command_serves_the_paste_again_after_a_stop_and_a_start(
    tmp_path: Path,
) -> None:
    """PRD.md Success and item 8 on the run path: unexpired pastes survive a restart.

    The service is stopped and started again as two runs of the documented
    command over one database file, which is what an operator does; the link the
    first run returned is opened against the second. The paste comes back
    byte-for-byte with the row — and the deadline — the first run stored, so
    nothing was lost and the downtime did not move an instant (PRD.md item 4).
    """
    with _running_service(tmp_path) as first_run:
        created = first_run.post(PASTES_PATH, OPERATOR_TEXT.encode("utf-8"))

        assert created.status_code == 201, created.text
        paste = created.json()

        # The link works in the run that issued it, so what the second run
        # answers below is the restart's doing and not the create route's
        # (PRD.md item 2).
        served = first_run.get(paste["url"])
        assert served.status_code == 200
        assert served.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert served.content == OPERATOR_TEXT.encode("utf-8")

        first_pid = first_run.process.pid

    # The first run is over: an operator's signal ended it cleanly, and its
    # work is in the file rather than in a process (PRD.md item 8).
    assert first_run.exit_code in CLEAN_EXIT_CODES
    assert first_run.process.poll() is not None

    rows_at_the_stop = first_run.rows()
    ((paste_id, text, created_at, expires_at),) = rows_at_the_stop
    assert (paste_id, text) == (paste["id"], OPERATOR_TEXT)
    assert expires_at - created_at == THREE_HOURS_SECONDS
    assert created_at <= time.time() < expires_at

    # The same command over the same file, as the operator starts it again.
    with _running_service(tmp_path) as second_run:
        assert second_run.process.poll() is None
        assert second_run.process.pid != first_pid

        # The link opened after the restart is the one the first run returned:
        # the same id on the new run's own origin.
        again = second_run.get(f"{PASTES_PATH}/{paste['id']}")

        assert again.status_code == 200
        assert again.headers["content-type"] == TEXT_PLAIN_CONTENT_TYPE
        assert again.content == OPERATOR_TEXT.encode("utf-8")

        # The restart neither lost the row nor gave the paste a fresh deadline:
        # the stored instants are the ones the first run wrote (PRD.md items 4
        # and 8).
        assert second_run.rows() == rows_at_the_stop
