from knowledge_workbench.config import Settings


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


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


def test_d3_non_file_ingestion_defaults_are_bounded() -> None:
    settings = Settings()
    assert settings.max_pasted_text_size_bytes == 1024 * 1024
    assert settings.web_fetch_connect_timeout_seconds == 5.0
    assert settings.web_fetch_total_timeout_seconds == 15.0
    assert settings.web_fetch_max_body_bytes == 5 * 1024 * 1024
    assert settings.web_fetch_max_redirects == 5


def test_d8_qa_defaults_are_bounded_and_reranker_is_opt_in() -> None:
    settings = _settings()
    assert settings.qa_candidate_top_k == 20
    assert settings.qa_rrf_k == 60
    assert settings.qa_context_max_chunks == 8
    assert settings.qa_context_max_characters == 14_000
    assert settings.qa_context_max_chunk_characters == 3_000
    assert settings.qa_max_output_tokens == 4_000
    assert settings.qa_timeout_seconds == 180.0
    assert settings.qa_processing_timeout_seconds == 240.0
    assert settings.qa_processing_sweep_interval_seconds == 60.0
    assert settings.reranker_provider == "disabled"
    assert settings.reranker_model == "BAAI/bge-reranker-v2-m3"


def test_d4_ai_defaults_do_not_require_credentials() -> None:
    settings = _settings()
    assert settings.ai_provider == "fake"
    assert settings.claude_model == "claude-opus-5"
    assert settings.ai_timeout_seconds == 120.0
    assert settings.extraction_max_output_tokens == 16000
