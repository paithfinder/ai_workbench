from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Zixu Knowledge Workbench API"
    app_version: str = "0.1.0"
    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:3000"]

    database_url: str = (
        "postgresql+asyncpg://knowledge:knowledge_dev@localhost:5432/knowledge_workbench"
    )
    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "knowledge-sources"
    s3_secure: bool = False
    s3_public_endpoint: str = "http://localhost:9000"
    s3_presign_ttl_seconds: int = Field(default=900, gt=0, le=604800)
    max_upload_size_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_pasted_text_size_bytes: int = Field(default=1 * 1024 * 1024, gt=0)
    knowledge_import_max_entries: int = Field(default=500, gt=0, le=2000)
    knowledge_import_max_folders: int = Field(default=250, gt=0, le=1000)
    knowledge_import_max_depth: int = Field(default=32, gt=0, le=64)
    knowledge_import_max_total_body_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    knowledge_import_max_document_characters: int = Field(default=20_000, gt=0)
    knowledge_import_max_relative_path_characters: int = Field(default=4_000, gt=0)
    knowledge_import_max_request_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    web_fetch_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    web_fetch_total_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    web_fetch_max_body_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    web_fetch_max_redirects: int = Field(default=5, ge=0, le=20)
    search_provider: Literal["disabled", "grok"] = "disabled"
    search_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    search_max_results: int = Field(default=10, gt=0, le=50)
    research_selection_max_urls: int = Field(default=10, gt=0, le=50)
    grok_search_endpoint: str = "https://api.x.ai/v1/responses"
    grok_search_model: str = Field(default="grok-4.1-fast", min_length=1, max_length=200)
    grok_search_api_key: SecretStr | None = None

    celery_broker_url: str = "redis://localhost:6379/1"
    job_attempt_lease_seconds: int = Field(default=300, gt=0)
    index_heartbeat_seconds: int = Field(default=30, gt=0)
    parse_attempt_lease_seconds: int = Field(default=1800, gt=0)
    parse_heartbeat_seconds: int = Field(default=30, gt=0)
    parse_timeout_seconds: int = Field(default=1800, gt=0)
    parse_max_pages: int = Field(default=200, gt=0)
    parse_max_source_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    parse_enable_ocr: bool = True
    parse_ocr_languages: list[str] = ["en", "zh"]
    parse_artifact_prefix: str = "artifacts"
    parser_name: Literal["docling"] = "docling"
    parser_version: str = "2.117.0"
    outbox_relay_interval_seconds: float = Field(default=1.0, gt=0)
    outbox_batch_size: int = Field(default=50, gt=0, le=500)

    chunker_version: str = "d7-structured-v1"
    chunk_target_characters: int = Field(default=1800, gt=100, le=10000)
    chunk_overlap_characters: int = Field(default=240, ge=0, le=2000)
    index_version: str = "d7-v1"
    embedding_provider: Literal["fake", "bge_m3_http"] = "fake"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimensions: Literal[1024] = 1024
    embedding_url: str = "http://embedding:8080/v1/embeddings"
    embedding_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    retrieval_default_top_k: int = Field(default=10, gt=0, le=100)

    qa_candidate_top_k: int = Field(default=20, gt=0, le=100)
    qa_rrf_k: int = Field(default=60, gt=0, le=1000)
    qa_context_max_chunks: int = Field(default=8, gt=0, le=50)
    qa_context_max_characters: int = Field(default=14_000, gt=0, le=100_000)
    qa_context_max_chunk_characters: int = Field(default=3_000, gt=0, le=20_000)
    qa_max_output_tokens: int = Field(default=4_000, gt=0, le=16_000)
    qa_timeout_seconds: float = Field(default=180.0, gt=0, le=600)
    qa_processing_timeout_seconds: float = Field(default=240.0, gt=0, le=3600)
    qa_processing_sweep_interval_seconds: float = Field(default=60.0, gt=0, le=600)
    reranker_provider: Literal["disabled", "fake", "bge_http"] = "disabled"
    reranker_url: str = "http://localhost:8081/rerank"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    ai_provider: Literal["fake", "anthropic"] = "fake"
    claude_model: str = Field(default="claude-opus-5", min_length=1)
    ai_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    extraction_max_output_tokens: int = Field(default=16000, gt=0, le=16000)
    extraction_batch_max_characters: int = Field(default=24000, gt=1000, le=100000)

    @model_validator(mode="after")
    def build_database_url_from_components(self) -> Settings:
        if "database_url" in self.model_fields_set:
            return self
        values = (
            self.postgres_host,
            self.postgres_db,
            self.postgres_user,
            self.postgres_password,
        )
        if not any(value is not None for value in values):
            return self
        if not all(value is not None for value in values):
            raise ValueError(
                "POSTGRES_HOST, POSTGRES_DB, POSTGRES_USER, and POSTGRES_PASSWORD "
                "must be configured together"
            )
        self.database_url = URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)
        return self

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
