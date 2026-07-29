"""Repository API contract tests with the service boundary isolated."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_workbench_api.api import repositories as repository_api
from ai_workbench_api.api.schemas import ApiError
from ai_workbench_api.domain.repositories import RepositorySummaryData
from ai_workbench_api.main import create_app
from ai_workbench_api.security.authorization_previews import AuthorizationPreview
from ai_workbench_api.security.repository_paths import RepositoryPathCandidate, RootIdentity
from conftest import make_settings

_PREVIEW_VALUE = "opaque-preview-value"


@pytest.mark.asyncio
async def test_preview_endpoint_returns_typed_envelope_without_writing_database() -> None:
    candidate = RepositoryPathCandidate(
        canonical_path=r"D:\workbench\safe-project",
        normalized_root_key=r"d:\workbench\safe-project",
        root_identity=RootIdentity("1:2"),
        display_name="safe-project",
    )
    service = MagicMock()
    service.preview.return_value = AuthorizationPreview(
        token=_PREVIEW_VALUE,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        candidate=candidate,
    )
    engine = MagicMock(spec=AsyncEngine)
    engine.dispose = AsyncMock()
    app = create_app(make_settings(), engine=engine, check_database_on_startup=False)
    app.dependency_overrides[repository_api._service] = lambda: service

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/repositories/local/authorization-previews",
                json={"path": r"D:\workbench\safe-project"},
                headers={"X-Request-ID": "preview-request"},
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == "preview-request"
    assert payload["error"] is None
    assert payload["data"]["state"] == "ready"
    assert payload["data"]["preview_token"] == _PREVIEW_VALUE
    assert payload["data"]["canonical_path"] == candidate.canonical_path
    service.preview.assert_called_once_with(r"D:\workbench\safe-project")


@pytest.mark.asyncio
async def test_authorization_requires_literal_true_confirmation() -> None:
    service = MagicMock()
    engine = MagicMock(spec=AsyncEngine)
    engine.dispose = AsyncMock()
    app = create_app(make_settings(), engine=engine, check_database_on_startup=False)
    app.dependency_overrides[repository_api._service] = lambda: service

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/repositories/local/authorizations",
                json={"preview_token": "opaque", "confirmation": False},
            )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "opaque" not in response.text
    service.authorize.assert_not_called()


@pytest.mark.asyncio
async def test_api_error_envelope_is_path_free_and_preserves_request_id() -> None:
    service = MagicMock()
    service.preview.side_effect = ApiError(
        400, "invalid_local_path", "The local repository path is invalid"
    )
    engine = MagicMock(spec=AsyncEngine)
    engine.dispose = AsyncMock()
    app = create_app(make_settings(), engine=engine, check_database_on_startup=False)
    app.dependency_overrides[repository_api._service] = lambda: service
    submitted_path = r"D:\private\customer-secret\source"

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/repositories/local/authorization-previews",
                json={"path": submitted_path},
                headers={"X-Request-ID": "safe-error-request"},
            )

    assert response.status_code == 400
    assert response.json() == {
        "data": None,
        "error": {
            "code": "invalid_local_path",
            "message": "The local repository path is invalid",
            "details": None,
        },
        "request_id": "safe-error-request",
    }
    assert submitted_path not in response.text


def test_openapi_documents_envelope_for_validation_and_domain_errors() -> None:
    app = create_app(make_settings(), check_database_on_startup=False)
    schema = app.openapi()
    operation = schema["paths"][
        "/api/v1/repositories/local/authorization-previews"
    ]["post"]

    for status in ("400", "409", "422", "500"):
        response_schema = operation["responses"][status]["content"]["application/json"][
            "schema"
        ]
        reference = response_schema["$ref"]
        assert "ApiEnvelope" in reference
    assert "HTTPValidationError" not in str(operation["responses"]["422"])


def _summary() -> RepositorySummaryData:
    return RepositorySummaryData(
        id=uuid4(),
        name="safe-project",
        canonical_root_path=r"D:\workbench\safe-project",
        authorization_status="authorized",
        authorization_epoch=1,
        authorized_at=datetime.now(UTC),
        revoked_at=None,
        scan_state="not_scanned",
        manifest_hash=None,
        eligible_files=0,
        eligible_bytes=0,
        indexing_state="not_queued",
    )
