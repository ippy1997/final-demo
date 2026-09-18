"""FastAPI application entry point.

Skeleton only: the app object and the health route that lets the test client
and the platform gate see the process start. Routes, error handlers and the
sweeper lifespan arrive with TASKS.md items 6, 7 and 9.
"""

from fastapi import FastAPI

app = FastAPI(title="Pastebin")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
