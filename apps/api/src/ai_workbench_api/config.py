"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:3000",
    "http://localhost:3000",
)


def _split_origins(value: object) -> object:
    if isinstance(value, str) and not value.lstrip().startswith("["):
        return [origin.strip() for origin in value.split(",") if origin.strip()]
    return value


CorsOrigins = Annotated[list[str], NoDecode, BeforeValidator(_split_origins)]


class Settings(BaseSettings):
    """Environment-backed settings; credentials remain in process memory only."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "AI Workbench API"
    app_version: str = "0.1.0"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    api_prefix: str = "/api/v1"
    database_url: PostgresDsn = Field(
        default_factory=lambda: PostgresDsn(
            "postgresql+asyncpg://ai_workbench@127.0.0.1:5432/ai_workbench"
        )
    )
    openai_api_key: SecretStr | None = None
    cors_origins: CorsOrigins = Field(default_factory=lambda: list(DEFAULT_CORS_ORIGINS))
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    repository_scan_max_directories: int = Field(default=5_000, ge=1, le=100_000)
    repository_scan_max_files: int = Field(default=20_000, ge=1, le=1_000_000)
    repository_scan_max_entries: int = Field(default=100_000, ge=1, le=2_000_000)
    repository_scan_max_depth: int = Field(default=40, ge=1, le=200)
    repository_scan_max_file_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1, le=100 * 1024 * 1024
    )
    repository_scan_max_total_bytes: int = Field(
        default=100 * 1024 * 1024, ge=1, le=10 * 1024 * 1024 * 1024
    )
    repository_scan_timeout_seconds: float = Field(default=30.0, gt=0, le=600)

    @property
    def sqlalchemy_database_url(self) -> str:
        """Return the DSN string for SQLAlchemy without exposing it in API output."""
        return self.database_url.encoded_string()

    @property
    def model_configured(self) -> bool:
        """Report provider readiness without exposing the provider credential."""
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value().strip())


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""
    return Settings()
