from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import Job, JobRetryRequest, JobStatus, OutboxEvent


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
        job.status = JobStatus.QUEUED.value
        job.progress = 0
        job.error_code = None
        job.error_message = None
        job.retryable = False
        job.started_at = None
        job.finished_at = None
        job.updated_at = datetime.now(UTC)
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
                event_type="job.source_ingest.requested",
                deduplication_key=f"source-ingest-retry:{request_id}",
                payload={"job_id": str(job.id), "space_id": str(space_id)},
            )
        )
        await session.flush()
        return job
