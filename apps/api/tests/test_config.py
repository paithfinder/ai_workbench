"""Configuration unit tests."""

from pytest import MonkeyPatch

from ai_workbench_api.config import DEFAULT_CORS_ORIGINS, Settings


def test_defaults_do_not_require_openai_key() -> None:
    settings = Settings()

    assert settings.openai_api_key is None
    assert settings.model_configured is False
    assert settings.cors_origins == list(DEFAULT_CORS_ORIGINS)
    assert settings.sqlalchemy_database_url.startswith("postgresql+asyncpg://")


def test_environment_style_values_are_parsed(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://127.0.0.1:4000,http://localhost:4000")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value-12345678")

    settings = Settings()

    assert settings.cors_origins == ["http://127.0.0.1:4000", "http://localhost:4000"]
    assert settings.openai_api_key is not None
    assert settings.model_configured is True
    assert "sk-test" not in repr(settings)
