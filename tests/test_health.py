"""The service starts and answers before any paste exists (TASKS.md item 1)."""

from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_200() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
