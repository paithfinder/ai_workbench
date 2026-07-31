from knowledge_workbench.config import Settings


def test_settings_parse_comma_separated_cors() -> None:
    settings = Settings(cors_origins="http://localhost:3000,http://127.0.0.1:3000")  # type: ignore[arg-type]
    assert settings.cors_origins == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_d1_ai_defaults_do_not_require_credentials() -> None:
    settings = Settings()
    assert settings.ai_provider == "fake"
    assert settings.claude_model == "claude-opus-5"
