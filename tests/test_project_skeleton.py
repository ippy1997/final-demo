"""Task 1 skeleton: the package, the documented start target and the manifests.

TASKS.md item 1 ships the ``app`` package with ``app/main.py``, the two
requirements files and ``tests/test_health.py``, and its success criterion is
that the service starts with one command as one process. The health route
itself is covered by ``tests/test_health.py``; this module checks the rest of
the deliverable, which no test asserted yet: that the ASGI target the
documented start command names (``app.main:app``) resolves to the FastAPI
application, and that the two dependency manifests declare the runtime and
test dependencies the task lists (ARCHITECTURE.md Stack, AGENTS.md Layout).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from fastapi import FastAPI

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]

# The ASGI target README.md, AGENTS.md and ARCHITECTURE.md start the one
# process with: `python -m uvicorn app.main:app`.
ASGI_TARGET = "app.main:app"


def _requirement_lines(filename: str) -> list[str]:
    """The declared lines of a requirements file, comments and blanks removed."""
    text = (REPO_ROOT / filename).read_text(encoding="utf-8")
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _requirement_names(filename: str) -> set[str]:
    """The distribution names a requirements file declares, normalised."""
    names = set()
    for line in _requirement_lines(filename):
        if line.startswith("-"):
            # An option such as `-r other.txt` or `-e .`, not a package.
            continue
        specifier = line.split(";", 1)[0].strip()
        name = re.split(r"[<>=!~\s\[]", specifier, maxsplit=1)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def test_tc001_documented_asgi_target_resolves_to_the_fastapi_app() -> None:
    """One-command start: `app.main:app` must load the FastAPI application."""
    module_name, _, attribute = ASGI_TARGET.partition(":")
    target = getattr(importlib.import_module(module_name), attribute)

    assert isinstance(target, FastAPI)
    assert target is app


def test_tc002_runtime_requirements_declare_fastapi_and_uvicorn() -> None:
    declared = _requirement_names("requirements.txt")

    assert {"fastapi", "uvicorn"} <= declared, (
        f"requirements.txt must declare fastapi and uvicorn, declared {sorted(declared)}"
    )


def test_tc003_dev_requirements_declare_pytest_and_httpx() -> None:
    declared = _requirement_names("requirements-dev.txt")

    assert {"pytest", "httpx"} <= declared, (
        f"requirements-dev.txt must declare pytest and httpx, declared {sorted(declared)}"
    )


def test_tc004_dev_requirements_install_the_runtime_requirements() -> None:
    """`pip install -r requirements-dev.txt` must also give the runtime deps."""
    includes_runtime = [
        line
        for line in _requirement_lines("requirements-dev.txt")
        if line.startswith("-r")
        and line[2:].strip().replace("\\", "/").split("/")[-1] == "requirements.txt"
    ]

    assert includes_runtime, (
        "requirements-dev.txt must include requirements.txt with a `-r` line"
    )


def test_tc005_runtime_requirements_leave_test_dependencies_to_the_dev_manifest() -> None:
    """The run install carries no test tooling (AGENTS.md Layout)."""
    leaked = {"pytest", "httpx"} & _requirement_names("requirements.txt")

    assert leaked == set(), (
        f"requirements.txt must not carry the test dependencies, found {sorted(leaked)}"
    )
