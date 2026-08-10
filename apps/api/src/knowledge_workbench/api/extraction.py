from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, status
from pydantic import BaseModel

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.api.jobs import JobResponse, _job_response
from knowledge_workbench.application.extraction import (
    CandidateRecord,
    ExtractionRecord,
    ExtractionService,
)
from knowledge_workbench.core.errors import ErrorEnvelope
from knowledge_workbench.db.models import CandidateStatus, ExtractionJob, SourceSection

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}", tags=["extraction"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 422, 503)
}


class ExtractionJobResponse(BaseModel):
    id: UUID
    job_id: UUID
    source_version_id: UUID
    parse_artifact_id: UUID
    status: str
    provider: str | None
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    provider_request_id: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class ScheduleExtractionResponse(BaseModel):
    job: JobResponse
    extraction: ExtractionJobResponse


class ExtractionRunResponse(BaseModel):
    job: JobResponse
    extraction: ExtractionJobResponse
    source_title: str


class ExtractionRunListResponse(BaseModel):
    items: list[ExtractionRunResponse]


class CandidateEvidenceResponse(BaseModel):
    section_id: UUID
    title: str | None
    text: str
    heading_path: list[str]
    page_number: int | None
    locator: dict[str, Any]
    quote_hash: str


class CandidateResponse(BaseModel):
    id: UUID
    extraction_job_id: UUID
    source_version_id: UUID
    source_title: str
    title: str
    body: str
    tags: list[str]
    suggested_destination_id: UUID | None
    atomicity: str
    confidence: float
    status: str
    version: int
    verification_reason: str | None
    rejection_reason: str | None
    reviewed_at: datetime | None
    conditions: list[str]
    exceptions: list[str]
    model: str
    prompt_version: str
    created_at: datetime
    evidence: list[CandidateEvidenceResponse]


class CandidateListResponse(BaseModel):
    items: list[CandidateResponse]


def _extraction_response(extraction: ExtractionJob) -> ExtractionJobResponse:
    return ExtractionJobResponse(
        id=extraction.id,
        job_id=extraction.job_id,
        source_version_id=extraction.source_version_id,
        parse_artifact_id=extraction.parse_artifact_id,
        status=extraction.status,
        provider=extraction.provider,
        model=extraction.model,
        prompt_version=extraction.prompt_version,
        input_tokens=extraction.input_tokens,
        output_tokens=extraction.output_tokens,
        latency_ms=extraction.latency_ms,
        provider_request_id=extraction.provider_request_id,
        error_code=extraction.error_code,
        error_message=extraction.error_message,
        started_at=extraction.started_at,
        completed_at=extraction.completed_at,
        created_at=extraction.created_at,
    )


def _extraction_run_response(record: ExtractionRecord) -> ExtractionRunResponse:
    return ExtractionRunResponse(
        job=_job_response(record.job),
        extraction=_extraction_response(record.extraction),
        source_title=record.source.title,
    )


def _evidence_response(section: SourceSection) -> CandidateEvidenceResponse:
    return CandidateEvidenceResponse(
        section_id=section.id,
        title=section.title,
        text=section.text,
        heading_path=section.heading_path,
        page_number=section.page_number,
        locator=section.locator,
        quote_hash=section.quote_hash,
    )


def _candidate_response(record: CandidateRecord) -> CandidateResponse:
    candidate = record.candidate
    return CandidateResponse(
        id=candidate.id,
        extraction_job_id=candidate.extraction_job_id,
        source_version_id=candidate.source_version_id,
        source_title=record.source.title,
        title=candidate.title,
        body=candidate.body,
        tags=candidate.tags,
        suggested_destination_id=candidate.suggested_destination_id,
        atomicity=candidate.atomicity,
        confidence=candidate.confidence,
        status=candidate.status,
        version=candidate.version,
        verification_reason=candidate.verification_reason,
        rejection_reason=candidate.rejection_reason,
        reviewed_at=candidate.reviewed_at,
        conditions=candidate.conditions,
        exceptions=candidate.exceptions,
        model=record.extraction.model,
        prompt_version=record.extraction.prompt_version,
        created_at=candidate.created_at,
        evidence=[_evidence_response(section) for section in record.evidence],
    )


@router.post(
    "/sources/{source_id}/versions/{version_id}/extractions",
    response_model=ScheduleExtractionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    operation_id="schedule_source_extraction",
)
async def schedule_extraction(
    space_id: UUID,
    source_id: UUID,
    version_id: UUID,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ScheduleExtractionResponse:
    async with session.begin():
        scheduled = await ExtractionService(settings).schedule(
            session,
            space_id=space_id,
            source_id=source_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
        )
    return ScheduleExtractionResponse(
        job=_job_response(scheduled.job),
        extraction=_extraction_response(scheduled.extraction),
    )


@router.get(
    "/sources/{source_id}/versions/{version_id}/extraction",
    response_model=ScheduleExtractionResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="get_source_extraction",
)
async def get_extraction(
    space_id: UUID,
    source_id: UUID,
    version_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ScheduleExtractionResponse:
    scheduled = await ExtractionService(settings).get_for_version(
        session,
        space_id=space_id,
        source_id=source_id,
        version_id=version_id,
    )
    return ScheduleExtractionResponse(
        job=_job_response(scheduled.job),
        extraction=_extraction_response(scheduled.extraction),
    )


@router.get(
    "/extractions",
    response_model=ExtractionRunListResponse,
    responses={422: {"model": ErrorEnvelope}},
    operation_id="list_source_extractions",
)
async def list_extractions(
    space_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ExtractionRunListResponse:
    records = await ExtractionService(settings).list_extractions(session, space_id=space_id)
    return ExtractionRunListResponse(
        items=[_extraction_run_response(record) for record in records]
    )


@router.get(
    "/candidates",
    response_model=CandidateListResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="list_extraction_candidates",
)
async def list_candidates(
    space_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
    candidate_status: Annotated[CandidateStatus | None, Query(alias="status")] = None,
) -> CandidateListResponse:
    records = await ExtractionService(settings).list_candidates(
        session, space_id=space_id, status=candidate_status
    )
    return CandidateListResponse(items=[_candidate_response(record) for record in records])
