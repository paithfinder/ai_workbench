"""FastAPI application factory and database-aware lifespan."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.cors import CORSMiddleware

from ai_workbench_api.api.health import router as health_router
from ai_workbench_api.api.middleware import (
    RequestContextMiddleware,
    api_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from ai_workbench_api.api.schemas import ApiError
from ai_workbench_api.config import Settings, get_settings
from ai_workbench_api.db.session import check_database, create_engine, create_session_factory
from ai_workbench_api.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    check_database_on_startup: bool = True,
) -> FastAPI:
    """Build an isolated application instance for production or tests."""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    resolved_engine = engine or create_engine(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved_settings
        app.state.engine = resolved_engine
        app.state.session_factory = create_session_factory(resolved_engine)
        app.state.database_connected = False
        try:
            if check_database_on_startup:
                try:
                    await check_database(
                        resolved_engine, resolved_settings.database_connect_timeout_seconds
                    )
                except Exception as exc:
                    logger.warning(
                        "database connectivity check failed",
                        extra={"error_type": type(exc).__name__},
                    )
                else:
                    app.state.database_connected = True
                    logger.info("database connectivity check succeeded")
            yield
        finally:
            app.state.database_connected = False
            await resolved_engine.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database_connected = False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Correlation-ID"],
        expose_headers=["X-Request-ID", "X-Correlation-ID"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.include_router(health_router, prefix=resolved_settings.api_prefix)
    return app


app = create_app()
