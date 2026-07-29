"""Health endpoint tests with database connectivity mocked at the boundary."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_workbench_api.main import create_app
from conftest import make_settings


@pytest.mark.asyncio
async def test_health_returns_uniform_non_sensitive_payload() -> None:
    settings = make_settings(
        app_name="Test API",
        app_version="9.9.9",
        database_url="postgresql+asyncpg://user:secret@127.0.0.1/test_db",
        openai_api_key=None,
    )
    engine = MagicMock(spec=AsyncEngine)
    engine.dispose = AsyncMock()

    with patch("ai_workbench_api.main.check_database", new=AsyncMock()) as database_check:
        app = create_app(settings, engine=engine)
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/v1/health",
                    headers={
                        "X-Request-ID": "request-123",
                        "X-Correlation-ID": "correlation-456",
                    },
                )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.headers["X-Correlation-ID"] == "correlation-456"
    assert response.json() == {
        "data": {
            "app": "Test API",
            "version": "9.9.9",
            "status": "ok",
            "database": "connected",
            "model_configured": False,
        },
        "error": None,
        "request_id": "request-123",
    }
    assert "secret" not in response.text
    database_check.assert_awaited_once_with(engine, settings.database_connect_timeout_seconds)
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_reports_degraded_when_database_check_fails() -> None:
    settings = make_settings()
    engine = MagicMock(spec=AsyncEngine)
    engine.dispose = AsyncMock()

    with patch(
        "ai_workbench_api.main.check_database",
        new=AsyncMock(side_effect=ConnectionError("credential in exception must stay private")),
    ):
        app = create_app(settings, engine=engine)
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "app": "AI Workbench API",
        "version": "0.1.0",
        "status": "degraded",
        "database": "unavailable",
        "model_configured": False,
    }
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cors_defaults_only_allow_local_frontend_origins() -> None:
    settings = make_settings()
    engine = MagicMock(spec=AsyncEngine)
    engine.dispose = AsyncMock()

    with patch("ai_workbench_api.main.check_database", new=AsyncMock()):
        app = create_app(settings, engine=engine)
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                allowed = await client.options(
                    "/api/v1/health",
                    headers={
                        "Origin": "http://127.0.0.1:3000",
                        "Access-Control-Request-Method": "GET",
                    },
                )
                denied = await client.options(
                    "/api/v1/health",
                    headers={
                        "Origin": "https://example.com",
                        "Access-Control-Request-Method": "GET",
                    },
                )

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    assert "access-control-allow-origin" not in denied.headers
