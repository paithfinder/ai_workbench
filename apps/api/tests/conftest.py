"""Shared typed settings helpers for API tests."""

from typing import Any

from pydantic import PostgresDsn

from ai_workbench_api.config import Settings

_TEST_DATABASE_URL = PostgresDsn(
    "postgresql+asyncpg://test-user:test-password@127.0.0.1:5432/test-db"
)


def make_settings(**overrides: Any) -> Settings:
    """Build settings without reading a developer's local environment file."""
    values: dict[str, Any] = {"database_url": _TEST_DATABASE_URL, **overrides}
    return Settings.model_validate(values)
