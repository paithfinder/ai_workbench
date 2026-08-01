from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio
from redis.asyncio import Redis

from knowledge_workbench.api import bootstrap, health, jobs, sources
from knowledge_workbench.config import Settings, get_settings
from knowledge_workbench.core.errors import install_error_handlers
from knowledge_workbench.core.logging import configure_logging
from knowledge_workbench.core.middleware import request_id_middleware
from knowledge_workbench.db.session import create_engine, create_session_factory
from knowledge_workbench.infrastructure.ai.fake import FakeAIGateway
from knowledge_workbench.infrastructure.storage.minio import MinioObjectStorage


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.redis.aclose()
        await app.state.engine.dispose()

    app = FastAPI(
        title="Zixu Knowledge Workbench API",
        version=resolved_settings.app_version,
        description="D2 source ingestion API for the personal knowledge management workbench.",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.engine = create_engine(resolved_settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.redis = Redis.from_url(resolved_settings.redis_url, decode_responses=True)
    app.state.storage = Minio(
        resolved_settings.s3_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=resolved_settings.s3_access_key,
        secret_key=resolved_settings.s3_secret_key,
        secure=resolved_settings.s3_secure,
    )
    signing_client = Minio(
        resolved_settings.s3_public_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=resolved_settings.s3_access_key,
        secret_key=resolved_settings.s3_secret_key,
        secure=resolved_settings.s3_public_endpoint.startswith("https://"),
    )
    app.state.object_storage = MinioObjectStorage(
        app.state.storage,
        signing_client,
        resolved_settings.s3_bucket,
        resolved_settings.s3_public_endpoint,
    )
    app.state.ai_gateway = FakeAIGateway()
    app.middleware("http")(request_id_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(bootstrap.router)
    app.include_router(sources.router)
    app.include_router(jobs.router)
    return app


app = create_app()
