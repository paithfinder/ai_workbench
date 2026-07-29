"""Request-scoped application dependencies."""

from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_workbench_api.db.session import session_scope
from ai_workbench_api.domain.repositories import ScanLockRegistry
from ai_workbench_api.security.authorization_previews import AuthorizationPreviewStore
from ai_workbench_api.security.repository_paths import RepositoryPathValidator
from ai_workbench_api.sources.local_repository_scanner import LocalRepositoryScanner, ScanLimits


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the app's request-scoped session with rollback-on-error semantics."""
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async for session in session_scope(factory):
        yield session


def get_path_validator(request: Request) -> RepositoryPathValidator:
    return cast(RepositoryPathValidator, request.app.state.repository_path_validator)


def get_preview_store(request: Request) -> AuthorizationPreviewStore:
    return cast(AuthorizationPreviewStore, request.app.state.authorization_preview_store)


def get_scanner(request: Request) -> LocalRepositoryScanner:
    return cast(LocalRepositoryScanner, request.app.state.local_repository_scanner)


def get_scan_locks(request: Request) -> ScanLockRegistry:
    return cast(ScanLockRegistry, request.app.state.scan_lock_registry)


def get_scan_limits(request: Request) -> ScanLimits:
    return cast(ScanLimits, request.app.state.repository_scan_limits)
