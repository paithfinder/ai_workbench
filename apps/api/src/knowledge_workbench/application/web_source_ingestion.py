from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.non_file_ingestion import (
    NonFileIngestionRecord,
    NonFileIngestionService,
)
from knowledge_workbench.application.ports.object_storage import ObjectStorage
from knowledge_workbench.application.ports.web_fetcher import WebFetcher
from knowledge_workbench.config import Settings
from knowledge_workbench.infrastructure.non_file_repository import (
    SqlAlchemyNonFileIngestionRepository,
)
from knowledge_workbench.infrastructure.web.safe_http import SafeHttpWebFetcher, normalize_web_url


class WebSourceFetcherFactory(Protocol):
    def __call__(self, settings: Settings) -> WebFetcher: ...


def create_web_fetcher(settings: Settings) -> WebFetcher:
    return SafeHttpWebFetcher(
        connect_timeout_seconds=settings.web_fetch_connect_timeout_seconds,
        total_timeout_seconds=settings.web_fetch_total_timeout_seconds,
        max_body_bytes=settings.web_fetch_max_body_bytes,
        max_redirects=settings.web_fetch_max_redirects,
    )


async def ingest_web_source(
    *,
    session: AsyncSession,
    storage: ObjectStorage,
    settings: Settings,
    space_id: UUID,
    title: str,
    url: str,
    idempotency_key: str,
    fetcher: WebFetcher | None = None,
) -> NonFileIngestionRecord:
    """Persist one web snapshot through the shared secure acquisition workflow."""
    requested_url = normalize_web_url(url)
    repository = SqlAlchemyNonFileIngestionRepository(session)
    service = NonFileIngestionService(
        storage,
        repository,
        max_pasted_text_bytes=settings.max_pasted_text_size_bytes,
    )
    async with session.begin():
        prepared = await service.prepare_web_source(
            space_id=space_id,
            title=title,
            requested_url=requested_url,
            idempotency_key=idempotency_key,
        )
    if prepared.claim.existing_record is not None:
        return prepared.claim.existing_record

    try:
        fetched = await (fetcher or create_web_fetcher(settings)).fetch(prepared.requested_url)
        staged = await service.stage_web_snapshot(prepared=prepared, fetched=fetched)
    except Exception:
        await _release_claim(
            session=session,
            repository=repository,
            space_id=space_id,
            idempotency_key=prepared.idempotency_key,
            lease_token=prepared.claim.lease_token,
        )
        raise
    try:
        async with session.begin():
            return await repository.finalize_web_snapshot(staged)
    except Exception:
        await service.discard_staged(staged.storage_key)
        await _release_claim(
            session=session,
            repository=repository,
            space_id=space_id,
            idempotency_key=prepared.idempotency_key,
            lease_token=prepared.claim.lease_token,
        )
        raise


async def _release_claim(
    *,
    session: AsyncSession,
    repository: SqlAlchemyNonFileIngestionRepository,
    space_id: UUID,
    idempotency_key: str,
    lease_token: UUID | None,
) -> None:
    if lease_token is None:
        return
    async with session.begin():
        await repository.release_claim(
            space_id=space_id,
            idempotency_key=idempotency_key,
            lease_token=lease_token,
        )
