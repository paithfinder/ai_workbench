from fastapi.testclient import TestClient

from knowledge_workbench.config import Settings
from knowledge_workbench.main import create_app


def test_bootstrap_reads_seeded_default_space_and_real_zero_counts() -> None:
    app = create_app(Settings(app_env="test"))
    with TestClient(app) as client:
        response = client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    assert response.json() == {
        "space": {
            "id": "01982ba0-4f20-7000-8000-000000000001",
            "slug": "my-knowledge-base",
            "name": "我的知识库",
        },
        "capabilities": {
            "source_import": True,
            "extraction_review": False,
            "knowledge_tree": False,
            "trusted_qa": False,
            "spaced_review": False,
            "evidence_agent": False,
        },
        "statistics": {
            "sources": 0,
            "queued_jobs": 0,
            "activity_events": 0,
        },
        "foundation_status": "ready",
    }
