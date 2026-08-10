from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    ExtractionJob,
    ExtractionStatus,
    Job,
    JobKind,
    JobRetryRequest,
    JobStatus,
    OutboxEvent,
    ParseArtifactStatus,
    ParseStatus,
    SourceParseArtifact,
    SourceVersion,
)
from knowledge_workbench.worker.job_runner import (
    UnsupportedJobKindError,
    requested_event_type,
)


class JobService:
    async def get_job(
        self, session: AsyncSession, *, space_id: UUID, job_id: UUID
    ) -> Job:
        job = await session.scalar(
            select(Job).where(Job.id == job_id, Job.space_id == space_id)
        )
        if job is None:
            raise AppError("job_not_found", "Job was not found.", status_code=404)
        return job

    async def retry_job(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        job_id: UUID,
        idempotency_key: str,
    ) -> Job:
        key = _validate_idempotency_key(idempotency_key)
        job = await session.scalar(
            select(Job)
            .where(Job.id == job_id, Job.space_id == space_id)
            .with_for_update()
        )
        if job is None:
            raise AppError("job_not_found", "Job was not found.", status_code=404)
        existing_retry = await session.scalar(
            select(JobRetryRequest).where(
                JobRetryRequest.job_id == job.id,
                JobRetryRequest.idempotency_key == key,
            )
        )
        if existing_retry is not None:
            return job
        if job.status != JobStatus.FAILED.value or not job.retryable:
            raise AppError(
                "job_not_retryable",
                "Only failed retryable jobs can be retried.",
                status_code=409,
            )
        try:
            job_kind = JobKind(job.kind)
            event_type = requested_event_type(job_kind)
        except (ValueError, UnsupportedJobKindError) as exc:
            raise AppError(
                "job_kind_not_retryable",
                "This job kind cannot be retried by the worker.",
                status_code=409,
            ) from exc
        job.status = JobStatus.QUEUED.value
        job.progress = 0
        job.error_code = None
        job.error_message = None
        job.retryable = False
        job.started_at = None
        job.finished_at = None
        job.attempt_budget_start = job.attempt_count
        job.updated_at = datetime.now(UTC)
        if job_kind == JobKind.SOURCE_PARSE:
            if job.source_version_id is None:
                raise AppError(
                    "parse_job_inconsistent",
                    "The parse job has no source version.",
                    status_code=500,
                )
            artifact = await session.scalar(
                select(SourceParseArtifact)
                .where(
                    SourceParseArtifact.source_version_id == job.source_version_id,
                    SourceParseArtifact.status == ParseArtifactStatus.FAILED.value,
                )
                .order_by(SourceParseArtifact.revision.desc())
                .with_for_update()
            )
            version = await session.scalar(
                select(SourceVersion)
                .where(SourceVersion.id == job.source_version_id)
                .with_for_update()
            )
            if artifact is None or version is None:
                raise AppError(
                    "parse_job_inconsistent",
                    "The failed parse revision is unavailable.",
                    status_code=500,
                )
            artifact.status = ParseArtifactStatus.QUEUED.value
            artifact.error_code = None
            artifact.error_message = None
            artifact.started_at = None
            artifact.completed_at = None
            version.parse_status = ParseStatus.QUEUED.value
        elif job_kind == JobKind.SOURCE_EXTRACT:
            extraction = await session.scalar(
                select(ExtractionJob)
                .where(ExtractionJob.job_id == job.id)
                .with_for_update()
            )
            if extraction is None:
                raise AppError(
                    "extraction_job_inconsistent",
                    "The failed extraction record is unavailable.",
                    status_code=500,
                )
            extraction.status = ExtractionStatus.QUEUED.value
            extraction.error_code = None
            extraction.error_message = None
            extraction.started_at = None
            extraction.completed_at = None
        next_attempt = job.attempt_count + 1
        request_id = uuid4()
        session.add(
            JobRetryRequest(
                id=request_id,
                job_id=job.id,
                idempotency_key=key,
                target_attempt_number=next_attempt,
            )
        )
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type=event_type,
                deduplication_key=f"{job_kind.value.replace('_', '-')}-retry:{request_id}",
                payload={"job_id": str(job.id), "space_id": str(space_id)},
            )
        )
        await session.flush()
        return job
