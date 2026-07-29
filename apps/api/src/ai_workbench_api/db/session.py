"""Async SQLAlchemy engine, session factory, and connectivity checks."""

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ai_workbench_api.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Create an async engine; schema creation is deliberately not performed."""
    return create_async_engine(
        settings.sqlalchemy_database_url,
        pool_pre_ping=True,
        connect_args={"timeout": settings.database_connect_timeout_seconds},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create request-scoped async sessions."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def check_database(engine: AsyncEngine, timeout_seconds: float) -> None:
    """Fail fast when PostgreSQL cannot be reached during application startup."""

    async def _check() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async with asyncio.timeout(timeout_seconds):
        await _check()


async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session and roll back unfinished work on failure."""
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
