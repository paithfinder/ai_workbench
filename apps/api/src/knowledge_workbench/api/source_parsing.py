from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, status
from pydantic import BaseModel

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.api.jobs import JobResponse, _job_response
from knowledge_workbench.api.sources import (
    SourceResponse,
    SourceVersionResponse,
    _source_response,
    _version_response,
)
from knowledge_workbench.application.source_parsing import SourceParsingService
from knowledge_workbench.core.errors import ErrorEnvelope
from knowledge_workbench.db.models import SourceParseArtifact, SourceSection

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}/sources", tags=["sources"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 413, 422, 503)
}


class ParseArtifactResponse(BaseModel):
    id: UUID
    revision: int
    status: str
    parser_name: str
    parser_version: str
    parser_config: dict[str, Any]
    page_count: int | None
    warnings: list[Any]
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class SourceVersionDetailResponse(BaseModel):
    version: SourceVersionResponse
    parse_job: JobResponse | None
    current_parse_artifact: ParseArtifactResponse | None
    section_count: int


class SourceDetailsResponse(BaseModel):
    source: SourceResponse
    versions: list[SourceVersionDetailResponse]


class ReparseResponse(BaseModel):
    job: JobResponse
    artifact: ParseArtifactResponse


class SectionResponse(BaseModel):
    id: UUID
    artifact_id: UUID
    artifact_revision: int
    ordinal: int
    block_id: str
    parent_block_id: str | None
    block_type: str
    title: str | None
    text: str
    heading_path: list[str]
    page_number: int | None
    paragraph_index: int | None
    bbox: dict[str, Any] | None
    locator: dict[str, Any]
    quote_hash: str
    content_hash: str
    provenance: dict[str, Any]


class SectionsResponse(BaseModel):
    items: list[SectionResponse]
    previous_cursor: str | None
    next_cursor: str | None
    artifact: ParseArtifactResponse


def _artifact_response(artifact: SourceParseArtifact) -> ParseArtifactResponse:
    return ParseArtifactResponse(
        id=artifact.id,
        revision=artifact.revision,
        status=artifact.status,
        parser_name=artifact.parser_name,
        parser_version=artifact.parser_version,
        parser_config=artifact.parser_config,
        page_count=artifact.page_count,
        warnings=artifact.warnings,
        error_code=artifact.error_code,
        error_message=artifact.error_message,
        started_at=artifact.started_at,
        completed_at=artifact.completed_at,
    )


def _section_response(section: SourceSection, revision: int) -> SectionResponse:
    return SectionResponse(
        id=section.id,
        artifact_id=section.parse_artifact_id,
        artifact_revision=revision,
        ordinal=section.ordinal,
        block_id=section.block_id,
        parent_block_id=section.parent_block_id,
        block_type=section.block_type,
        title=section.title,
        text=section.text,
        heading_path=section.heading_path,
        page_number=section.page_number,
        paragraph_index=section.paragraph_index,
        bbox=section.bbox,
        locator=section.locator,
        quote_hash=section.quote_hash,
        content_hash=section.content_hash,
        provenance=section.provenance,
    )


@router.get(
    "/{source_id}/details",
    response_model=SourceDetailsResponse,
    responses=ERROR_RESPONSES,
    operation_id="get_source_details",
)
async def source_details(
    space_id: UUID,
    source_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> SourceDetailsResponse:
    source, rows = await SourceParsingService(settings).source_details(
        session, space_id=space_id, source_id=source_id
    )
    return SourceDetailsResponse(
        source=_source_response(source),
        versions=[
            SourceVersionDetailResponse(
                version=_version_response(version),
                parse_job=_job_response(job) if job else None,
                current_parse_artifact=_artifact_response(artifact) if artifact else None,
                section_count=count,
            )
            for version, job, artifact, count in rows
        ],
    )


@router.get(
    "/{source_id}/versions/{version_id}/sections",
    response_model=SectionsResponse,
    responses=ERROR_RESPONSES,
    operation_id="list_source_sections",
)
async def list_sections(
    space_id: UUID,
    source_id: UUID,
    version_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    artifact_id: Annotated[UUID | None, Query()] = None,
    anchor_section_id: Annotated[UUID | None, Query()] = None,
) -> SectionsResponse:
    page = await SourceParsingService(settings).list_sections(
        session,
        space_id=space_id,
        source_id=source_id,
        version_id=version_id,
        limit=limit,
        cursor=cursor,
        artifact_id=artifact_id,
        anchor_section_id=anchor_section_id,
    )
    return SectionsResponse(
        items=[_section_response(item, page.artifact.revision) for item in page.items],
        previous_cursor=page.previous_cursor,
        next_cursor=page.next_cursor,
        artifact=_artifact_response(page.artifact),
    )


@router.post(
    "/{source_id}/versions/{version_id}/reparse",
    response_model=ReparseResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    operation_id="reparse_source_version",
)
async def reparse(
    space_id: UUID,
    source_id: UUID,
    version_id: UUID,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ReparseResponse:
    async with session.begin():
        scheduled = await SourceParsingService(settings).reparse(
            session,
            space_id=space_id,
            source_id=source_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
        )
    return ReparseResponse(
        job=_job_response(scheduled.job), artifact=_artifact_response(scheduled.artifact)
    )
