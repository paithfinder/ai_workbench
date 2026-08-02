from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import BaseModel, Field

from knowledge_workbench.api.dependencies import (
    SessionDependency,
    SettingsDependency,
    StorageDependency,
)
from knowledge_workbench.api.sources import (
    JobReferenceResponse,
    SourceResponse,
    SourceVersionResponse,
    _job_reference,
    _source_response,
    _version_response,
)
from knowledge_workbench.application.non_file_ingestion import (
    NonFileIngestionRecord,
    NonFileIngestionService,
)
from knowledge_workbench.core.errors import ErrorEnvelope
from knowledge_workbench.infrastructure.non_file_repository import (
    SqlAlchemyNonFileIngestionRepository,
)
from knowledge_workbench.infrastructure.web.safe_http import (
    SafeHttpWebFetcher,
    normalize_web_url,
)

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}/sources", tags=["sources"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 413, 422, 503)
}


class PastedTextRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1)


class WebSourceRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2000)


class NonFileSourceResponse(BaseModel):
    source: SourceResponse
    version: SourceVersionResponse
    job: JobReferenceResponse


def _response(result: Any) -> NonFileSourceResponse:
    return NonFileSourceResponse(
        source=_source_response(result.source),
        version=_version_response(result.version),
        job=_job_reference(result.job),
    )


async def _finalize_and_load(
    *,
    repository: SqlAlchemyNonFileIngestionRepository,
    record: NonFileIngestionRecord,
    space_id: UUID,
) -> NonFileSourceResponse:
    result = await repository.result(
        space_id=space_id,
        source_id=record.source_id,
        version_id=record.version_id,
        job_id=record.job_id,
    )
    return _response(result)


async def _release_claim(
    *,
    session: Any,
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


@router.post(
    "/pasted-text",
    response_model=NonFileSourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    operation_id="create_pasted_text_source",
)
async def create_pasted_text_source(
    space_id: UUID,
    body: PastedTextRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> NonFileSourceResponse:
    repository = SqlAlchemyNonFileIngestionRepository(session)
    service = NonFileIngestionService(
        storage,
        repository,
        max_pasted_text_bytes=settings.max_pasted_text_size_bytes,
    )
    async with session.begin():
        prepared = await service.prepare_pasted_text(
            space_id=space_id,
            title=body.title,
            text=body.text,
            idempotency_key=idempotency_key,
        )
    if prepared.claim.existing_record is not None:
        async with session.begin():
            return await _finalize_and_load(
                repository=repository,
                record=prepared.claim.existing_record,
                space_id=space_id,
            )

    try:
        staged = await service.stage_pasted_text(prepared)
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
            record = await repository.finalize_pasted_text(staged)
            return await _finalize_and_load(
                repository=repository,
                record=record,
                space_id=space_id,
            )
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


@router.post(
    "/web",
    response_model=NonFileSourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    operation_id="create_web_source",
)
async def create_web_source(
    space_id: UUID,
    body: WebSourceRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> NonFileSourceResponse:
    requested_url = normalize_web_url(body.url)
    repository = SqlAlchemyNonFileIngestionRepository(session)
    service = NonFileIngestionService(
        storage,
        repository,
        max_pasted_text_bytes=settings.max_pasted_text_size_bytes,
    )
    async with session.begin():
        prepared = await service.prepare_web_source(
            space_id=space_id,
            title=body.title,
            requested_url=requested_url,
            idempotency_key=idempotency_key,
        )
    if prepared.claim.existing_record is not None:
        async with session.begin():
            return await _finalize_and_load(
                repository=repository,
                record=prepared.claim.existing_record,
                space_id=space_id,
            )

    try:
        fetched = await SafeHttpWebFetcher(
            connect_timeout_seconds=settings.web_fetch_connect_timeout_seconds,
            total_timeout_seconds=settings.web_fetch_total_timeout_seconds,
            max_body_bytes=settings.web_fetch_max_body_bytes,
            max_redirects=settings.web_fetch_max_redirects,
        ).fetch(prepared.requested_url)
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
            record = await repository.finalize_web_snapshot(staged)
            return await _finalize_and_load(
                repository=repository,
                record=record,
                space_id=space_id,
            )
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
