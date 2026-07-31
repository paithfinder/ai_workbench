from fastapi.testclient import TestClient

from knowledge_workbench.config import Settings
from knowledge_workbench.main import create_app


def test_liveness_returns_request_id() -> None:
    app = create_app(Settings(app_env="test"))
    with TestClient(app) as client:
        response = client.get(
            "/health/live",
            headers={"X-Request-ID": "550e8400-e29b-41d4-a716-446655440000"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "550e8400-e29b-41d4-a716-446655440000"
    assert response.json()["status"] == "ok"
