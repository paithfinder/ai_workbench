from knowledge_workbench.config import Settings


def test_settings_parse_comma_separated_cors() -> None:
    settings = Settings(cors_origins="http://localhost:3000,http://127.0.0.1:3000")  # type: ignore[arg-type]
    assert settings.cors_origins == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_database_url_components_encode_reserved_password_characters() -> None:
    settings = Settings(
        postgres_host="postgres",
        postgres_db="knowledge_workbench",
        postgres_user="knowledge",
        postgres_password="p@ss:/#word",
    )
    assert settings.database_url == (
        "postgresql+asyncpg://knowledge:p%40ss%3A%2F%23word@postgres:5432/knowledge_workbench"
    )


def test_explicit_database_url_takes_priority_over_components() -> None:
    explicit = "postgresql+asyncpg://explicit:secret@database:5432/custom"
    settings = Settings(
        database_url=explicit,
        postgres_host="ignored",
        postgres_db="ignored",
        postgres_user="ignored",
        postgres_password="ignored",
    )
    assert settings.database_url == explicit


def test_d2_ingestion_defaults_are_bounded() -> None:
    settings = Settings()
    assert settings.max_upload_size_bytes == 25 * 1024 * 1024
    assert settings.s3_presign_ttl_seconds == 900
    assert settings.celery_broker_url.endswith("/1")


def test_d1_ai_defaults_do_not_require_credentials() -> None:
    settings = Settings()
    assert settings.ai_provider == "fake"
    assert settings.claude_model == "claude-opus-5"
