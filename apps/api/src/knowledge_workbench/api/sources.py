from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import BaseModel, Field

from knowledge_workbench.api.dependencies import (
    SessionDependency,
    SettingsDependency,
    StorageDependency,
)
from knowledge_workbench.application.source_ingestion import (
    SourceIngestionService,
    UploadDeclaration,
)
from knowledge_workbench.core.errors import ErrorEnvelope
from knowledge_workbench.db.models import Job, Source, SourceKind, SourceVersion

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope}
    for code in (404, 409, 413, 422, 503)
}

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}/sources", tags=["sources"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]


class CreateSourceRequest(BaseModel):
    kind: Literal[SourceKind.PDF, SourceKind.MARKDOWN, SourceKind.TEXT]
    title: str = Field(min_length=1, max_length=500)


class SourceResponse(BaseModel):
    id: UUID
    space_id: UUID
    kind: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


class SourceListResponse(BaseModel):
    items: list[SourceResponse]


class ReserveUploadRequest(BaseModel):
    original_filename: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceVersionResponse(BaseModel):
    id: UUID
    source_id: UUID
    version_number: int
    original_filename: str
    media_type: str
    size_bytes: int
    content_sha256: str | None
    processing_status: str
    parse_status: str
    upload_expires_at: datetime
    completed_at: datetime | None
    created_at: datetime


class SourceVersionListResponse(BaseModel):
    items: list[SourceVersionResponse]


class UploadReservationResponse(BaseModel):
    version: SourceVersionResponse
    upload_url: str
    upload_headers: dict[str, str]
    upload_fields: dict[str, str]
    expires_at: datetime


class JobReferenceResponse(BaseModel):
    id: UUID
    status: str


class CompleteUploadResponse(BaseModel):
    source: SourceResponse
    version: SourceVersionResponse
    job: JobReferenceResponse


def _source_response(source: Source) -> SourceResponse:
    return SourceResponse(
        id=source.id,
        space_id=source.space_id,
        kind=source.kind,
        title=source.title,
        status=source.status,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _version_response(version: SourceVersion) -> SourceVersionResponse:
    return SourceVersionResponse(
        id=version.id,
        source_id=version.source_id,
        version_number=version.version_number,
        original_filename=version.original_filename,
        media_type=version.media_type,
        size_bytes=version.size_bytes,
        content_sha256=version.content_sha256,
        processing_status=version.processing_status,
        parse_status=version.parse_status,
        upload_expires_at=version.upload_expires_at,
        completed_at=version.completed_at,
        created_at=version.created_at,
    )


def _job_reference(job: Job) -> JobReferenceResponse:
    return JobReferenceResponse(id=job.id, status=job.status)


@router.post(
    "",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
    operation_id="create_source",
)
async def create_source(
    space_id: UUID,
    body: CreateSourceRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> SourceResponse:
    async with session.begin():
        source = await SourceIngestionService(storage, settings).create_source(
            session,
            space_id=space_id,
            kind=body.kind,
            title=body.title,
            idempotency_key=idempotency_key,
        )
    return _source_response(source)


@router.get(
    "",
    response_model=SourceListResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="list_sources",
)
async def list_sources(
    space_id: UUID,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> SourceListResponse:
    sources = await SourceIngestionService(storage, settings).list_sources(
        session, space_id=space_id
    )
    return SourceListResponse(items=[_source_response(source) for source in sources])


@router.get(
    "/{source_id}",
    response_model=SourceResponse,
    responses={
        404: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    },
    operation_id="get_source",
)
async def get_source(
    space_id: UUID,
    source_id: UUID,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> SourceResponse:
    source = await SourceIngestionService(storage, settings).get_source(
        session, space_id=space_id, source_id=source_id
    )
    return _source_response(source)


@router.get(
    "/{source_id}/versions",
    response_model=SourceVersionListResponse,
    responses={
        404: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    },
    operation_id="list_source_versions",
)
async def list_source_versions(
    space_id: UUID,
    source_id: UUID,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> SourceVersionListResponse:
    versions = await SourceIngestionService(storage, settings).list_versions(
        session, space_id=space_id, source_id=source_id
    )
    return SourceVersionListResponse(
        items=[_version_response(version) for version in versions]
    )


@router.post(
    "/{source_id}/upload-reservations",
    response_model=UploadReservationResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
    operation_id="reserve_source_upload",
)
async def reserve_upload(
    space_id: UUID,
    source_id: UUID,
    body: ReserveUploadRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> UploadReservationResponse:
    async with session.begin():
        reservation = await SourceIngestionService(storage, settings).reserve_upload(
            session,
            space_id=space_id,
            source_id=source_id,
            declaration=UploadDeclaration(
                original_filename=body.original_filename,
                media_type=body.media_type,
                size_bytes=body.size_bytes,
                content_sha256=body.content_sha256,
            ),
            idempotency_key=idempotency_key,
        )
    return UploadReservationResponse(
        version=_version_response(reservation.version),
        upload_url=reservation.upload.url,
        upload_headers=reservation.upload.headers,
        upload_fields=reservation.upload.fields,
        expires_at=reservation.version.upload_expires_at,
    )


@router.post(
    "/{source_id}/versions/{version_id}/complete",
    response_model=CompleteUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    operation_id="complete_source_upload",
)
async def complete_upload(
    space_id: UUID,
    source_id: UUID,
    version_id: UUID,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> CompleteUploadResponse:
    async with session.begin():
        completed = await SourceIngestionService(storage, settings).complete_upload(
            session,
            space_id=space_id,
            source_id=source_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
        )
    return CompleteUploadResponse(
        source=_source_response(completed.source),
        version=_version_response(completed.version),
        job=_job_reference(completed.job),
    )
