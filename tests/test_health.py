"""The service starts and answers before any paste exists (TASKS.md item 1)."""

from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_200() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_health_returns_the_ok_payload_as_json() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_health_is_served_by_the_documented_asgi_target() -> None:
    """`app.main:app` is the target README.md and ARCHITECTURE.md start."""
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "get" in paths["/health"]
