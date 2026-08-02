from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_workbench.application.source_parsing import SourceParsingService
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobAttempt,
    JobAttemptStatus,
    JobKind,
    JobStatus,
    ProcessingStatus,
    Source,
    SourceStatus,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine, create_session_factory
from knowledge_workbench.worker.job_runner import (
    ActiveJobLeaseError as ActiveJobLeaseError,
)
from knowledge_workbench.worker.job_runner import (
    ClaimToken,
    JobAttemptError,
    JobRunner,
    PermanentJobError,
)
from knowledge_workbench.worker.job_runner import (
    mark_transient_failure as mark_job_transient_failure,
)
from knowledge_workbench.worker.job_runner import (
    mark_transient_failure_sync as mark_job_transient_failure_sync,
)
from knowledge_workbench.worker.job_runner import (
    recover_expired_leases as recover_expired_leases,
)
from knowledge_workbench.worker.job_runner import (
    recover_orphaned_queued_jobs as recover_orphaned_queued_jobs,
)

# Backwards-compatible names used by the existing Celery task and unit tests.
PermanentIngestError = PermanentJobError
IngestAttemptError = JobAttemptError


class SourceIngestWorker(JobRunner):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, JobKind.SOURCE_INGEST)

    async def _complete(
        self, session: AsyncSession, *, job_id: UUID, token: ClaimToken
    ) -> None:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if (
                job is None
                or job.kind != JobKind.SOURCE_INGEST.value
                or job.status != JobStatus.RUNNING.value
                or job.attempt_count != token.attempt_number
            ):
                return
            current_attempt = await session.scalar(
                select(JobAttempt)
                .where(
                    JobAttempt.id == token.attempt_id,
                    JobAttempt.job_id == job.id,
                    JobAttempt.attempt_number == token.attempt_number,
                    JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    JobAttempt.lease_expires_at > datetime.now(UTC),
                )
                .with_for_update()
            )
            if current_attempt is None:
                return
            if job.source_version_id is None:
                raise PermanentIngestError(
                    "source_version_missing", "The ingestion job has no source version."
                )
            version = await session.scalar(
                select(SourceVersion).where(SourceVersion.id == job.source_version_id)
            )
            if version is None:
                raise PermanentIngestError(
                    "source_version_not_found", "The source version was not found."
                )
            if version.completed_at is None or version.storage_key is None:
                raise PermanentIngestError(
                    "source_version_not_completed",
                    "The source version is not ready for ingestion.",
                )
            source = await session.scalar(
                select(Source).where(
                    Source.id == version.source_id,
                    Source.space_id == job.space_id,
                )
            )
            if source is None:
                raise PermanentIngestError(
                    "source_not_found", "The source was not found for ingestion."
                )
            now = datetime.now(UTC)
            source.status = SourceStatus.ACTIVE.value
            source.updated_at = now
            version.processing_status = ProcessingStatus.READY.value
            scheduled = await SourceParsingService(self._settings).schedule_initial_parse(
                session,
                source=source,
                version=version,
            )
            del scheduled
            job.status = JobStatus.SUCCEEDED.value
            job.progress = 100
            job.retryable = False
            job.error_code = None
            job.error_message = None
            job.finished_at = now
            job.updated_at = now
            current_attempt.status = JobAttemptStatus.SUCCEEDED.value
            current_attempt.heartbeat_at = now
            current_attempt.lease_expires_at = now
            current_attempt.finished_at = now

    async def _on_failed(self, session: AsyncSession, *, job: Job, now: datetime) -> None:
        if job.source_version_id is None:
            return
        version = await session.scalar(
            select(SourceVersion).where(SourceVersion.id == job.source_version_id)
        )
        if version is None:
            return
        version.processing_status = ProcessingStatus.FAILED.value
        source = await session.scalar(select(Source).where(Source.id == version.source_id))
        if source is not None:
            source.status = SourceStatus.FAILED.value
            source.updated_at = now


async def mark_transient_failure(
    settings: Settings,
    *,
    job_id: UUID,
    token: ClaimToken,
    message: str,
) -> None:
    await mark_job_transient_failure(
        settings,
        job_kind=JobKind.SOURCE_INGEST,
        job_id=job_id,
        token=token,
        message=message,
        error_code="source_ingest_transient_failure",
    )


def mark_transient_failure_sync(
    settings: Settings,
    *,
    job_id: UUID,
    token: ClaimToken,
    message: str,
) -> None:
    mark_job_transient_failure_sync(
        settings,
        job_kind=JobKind.SOURCE_INGEST,
        job_id=job_id,
        token=token,
        message=message,
        error_code="source_ingest_transient_failure",
    )


async def run_source_ingest(
    settings: Settings,
    *,
    job_id: UUID,
    celery_task_id: str | None,
    worker_name: str | None,
) -> None:
    engine = create_engine(settings)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await SourceIngestWorker(settings).run(
                session,
                job_id=job_id,
                celery_task_id=celery_task_id,
                worker_name=worker_name,
            )
    finally:
        await engine.dispose()


def run_source_ingest_sync(
    settings: Settings,
    *,
    job_id: UUID,
    celery_task_id: str | None,
    worker_name: str | None,
) -> None:
    asyncio.run(
        run_source_ingest(
            settings,
            job_id=job_id,
            celery_task_id=celery_task_id,
            worker_name=worker_name,
        )
    )
