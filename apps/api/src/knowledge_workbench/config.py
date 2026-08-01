from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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

    celery_broker_url: str = "redis://localhost:6379/1"
    job_attempt_lease_seconds: int = Field(default=300, gt=0)
    outbox_relay_interval_seconds: float = Field(default=1.0, gt=0)
    outbox_batch_size: int = Field(default=50, gt=0, le=500)

    ai_provider: Literal["fake"] = "fake"
    claude_model: str = Field(default="claude-opus-5", min_length=1)

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
