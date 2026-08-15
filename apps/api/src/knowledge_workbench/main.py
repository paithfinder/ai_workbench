from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_workbench.api import (
    bootstrap,
    candidate_review,
    extraction,
    health,
    jobs,
    knowledge_import,
    knowledge_tree,
    knowledge_update_proposal,
    non_file_sources,
    qa,
    retrieval,
    source_parsing,
    sources,
)
from knowledge_workbench.application.qa_persistence import expire_stale_processing_turns
from knowledge_workbench.config import Settings, get_settings
from knowledge_workbench.core.errors import install_error_handlers
from knowledge_workbench.core.logging import configure_logging, get_logger
from knowledge_workbench.core.middleware import (
    knowledge_import_body_limit_middleware,
    pasted_text_body_limit_middleware,
    request_id_middleware,
)
from knowledge_workbench.db.session import create_engine, create_session_factory
from knowledge_workbench.infrastructure.storage.minio import MinioObjectStorage

logger = get_logger(component="qa_processing_sweeper")


async def sweep_stale_qa_turns(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    timeout_seconds: float,
    interval_seconds: float,
) -> None:
    while True:
        try:
            await expire_stale_processing_turns(
                session_factory,
                timeout_seconds=timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "qa_processing_sweep_failed",
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(interval_seconds)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sweep_task = asyncio.create_task(
            sweep_stale_qa_turns(
                app.state.session_factory,
                timeout_seconds=resolved_settings.qa_processing_timeout_seconds,
                interval_seconds=resolved_settings.qa_processing_sweep_interval_seconds,
            )
        )
        try:
            yield
        finally:
            sweep_task.cancel()
            await asyncio.gather(sweep_task, return_exceptions=True)
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(
        title="Zixu Knowledge Workbench API",
        version=resolved_settings.app_version,
        description=(
            "D5 immutable source ingestion, parsing, structured AI extraction, and "
            "human candidate review API for the personal knowledge workbench."
        ),
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
    app.middleware("http")(
        pasted_text_body_limit_middleware(resolved_settings.max_pasted_text_size_bytes)
    )
    app.middleware("http")(
        knowledge_import_body_limit_middleware(
            resolved_settings.knowledge_import_max_request_bytes
        )
    )
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
    app.include_router(non_file_sources.router)
    app.include_router(source_parsing.router)
    app.include_router(extraction.router)
    app.include_router(candidate_review.router)
    app.include_router(knowledge_tree.router)
    app.include_router(knowledge_import.router)
    app.include_router(knowledge_update_proposal.router)
    app.include_router(retrieval.router)
    app.include_router(qa.router)
    app.include_router(jobs.router)
    return app


app = create_app()
