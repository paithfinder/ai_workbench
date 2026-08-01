from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import BaseModel

from knowledge_workbench.api.dependencies import SessionDependency
from knowledge_workbench.application.jobs import JobService
from knowledge_workbench.core.errors import ErrorEnvelope
from knowledge_workbench.db.models import Job

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}/jobs", tags=["jobs"])
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope}
    for code in (404, 409, 422, 503)
}
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]


class JobResponse(BaseModel):
    id: UUID
    space_id: UUID
    source_version_id: UUID | None
    kind: str
    status: str
    progress: int
    attempt_count: int
    retryable: bool
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _job_response(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        space_id=job.space_id,
        source_version_id=job.source_version_id,
        kind=job.kind,
        status=job.status,
        progress=job.progress,
        attempt_count=job.attempt_count,
        retryable=job.retryable,
        error_code=job.error_code,
        error_message=job.error_message,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    responses={
        404: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    },
    operation_id="get_job",
)
async def get_job(
    space_id: UUID,
    job_id: UUID,
    session: SessionDependency,
) -> JobResponse:
    job = await JobService().get_job(session, space_id=space_id, job_id=job_id)
    return _job_response(job)


@router.post(
    "/{job_id}/retry",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    operation_id="retry_job",
)
async def retry_job(
    space_id: UUID,
    job_id: UUID,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> JobResponse:
    async with session.begin():
        job = await JobService().retry_job(
            session,
            space_id=space_id,
            job_id=job_id,
            idempotency_key=idempotency_key,
        )
    return _job_response(job)
