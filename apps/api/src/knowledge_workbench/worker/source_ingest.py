from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobAttempt,
    JobAttemptStatus,
    JobStatus,
    OutboxEvent,
    ParseStatus,
    ProcessingStatus,
    Source,
    SourceStatus,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine, create_session_factory

MAX_AUTOMATIC_ATTEMPTS = 8


class ActiveJobLeaseError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("Another worker owns the active job lease")


class PermanentIngestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ClaimToken:
    def __init__(self, attempt_id: UUID, attempt_number: int) -> None:
        self.attempt_id = attempt_id
        self.attempt_number = attempt_number


class IngestAttemptError(Exception):
    def __init__(self, token: ClaimToken, message: str) -> None:
        self.token = token
        super().__init__(message)


class SourceIngestWorker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        celery_task_id: str | None,
        worker_name: str | None,
    ) -> None:
        token: ClaimToken | None = None
        try:
            token = await self._claim(
                session,
                job_id=job_id,
                celery_task_id=celery_task_id,
                worker_name=worker_name,
            )
            if token is None:
                return
            try:
                if not await self._heartbeat(session, job_id=job_id, token=token):
                    return
                await self._complete(session, job_id=job_id, token=token)
            except PermanentIngestError:
                raise
            except Exception as exc:
                raise IngestAttemptError(token, str(exc)) from exc
        except ActiveJobLeaseError:
            raise
        except PermanentIngestError as exc:
            await session.rollback()
            if token is not None:
                await self._mark_failed(
                    session,
                    job_id=job_id,
                    token=token,
                    code=exc.code,
                    message=str(exc),
                    retryable=False,
                )
        except SQLAlchemyError:
            await session.rollback()
            raise
        except IngestAttemptError:
            await session.rollback()
            raise
        except Exception:
            await session.rollback()
            raise

    async def _claim(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        celery_task_id: str | None,
        worker_name: str | None,
    ) -> ClaimToken | None:
        async with session.begin():
            job = await session.scalar(
                select(Job).where(Job.id == job_id).with_for_update()
            )
            if job is None or job.status == JobStatus.SUCCEEDED.value:
                return None
            now = datetime.now(UTC)
            if job.status == JobStatus.RUNNING.value:
                active = await session.scalar(
                    select(JobAttempt)
                    .where(
                        JobAttempt.job_id == job.id,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    )
                    .order_by(JobAttempt.attempt_number.desc())
                    .with_for_update()
                )
                if active is not None and active.lease_expires_at > now:
                    remaining = int((active.lease_expires_at - now).total_seconds()) + 1
                    raise ActiveJobLeaseError(remaining)
                if active is not None:
                    active.status = JobAttemptStatus.FAILED.value
                    active.finished_at = now
                    active.error_code = "lease_expired"
                    active.error_message = "The worker lease expired before completion."
                if job.attempt_count >= MAX_AUTOMATIC_ATTEMPTS:
                    job.retryable = True
                    await self._set_ingest_failed(
                        session,
                        job=job,
                        code="automatic_attempts_exhausted",
                        message="Automatic ingestion recovery attempts were exhausted.",
                        now=now,
                    )
                    return None
            if job.status not in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
                return None
            attempt_number = job.attempt_count + 1
            job.status = JobStatus.RUNNING.value
            job.progress = 10
            job.attempt_count = attempt_number
            job.started_at = job.started_at or now
            job.finished_at = None
            job.error_code = None
            job.error_message = None
            job.retryable = False
            job.updated_at = now
            attempt_id = uuid4()
            session.add(
                JobAttempt(
                    id=attempt_id,
                    job_id=job.id,
                    attempt_number=attempt_number,
                    status=JobAttemptStatus.RUNNING.value,
                    celery_task_id=celery_task_id,
                    worker_name=worker_name,
                    started_at=now,
                    heartbeat_at=now,
                    lease_expires_at=now
                    + timedelta(seconds=self._settings.job_attempt_lease_seconds),
                )
            )
            await session.flush()
        return ClaimToken(attempt_id, attempt_number)

    async def _heartbeat(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        token: ClaimToken,
    ) -> bool:
        async with session.begin():
            job = await session.scalar(
                select(Job).where(Job.id == job_id).with_for_update()
            )
            if (
                job is None
                or job.status != JobStatus.RUNNING.value
                or job.attempt_count != token.attempt_number
            ):
                return False
            attempt = await session.scalar(
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
            if attempt is None:
                return False
            now = datetime.now(UTC)
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now + timedelta(
                seconds=self._settings.job_attempt_lease_seconds
            )
            job.updated_at = now
            await session.flush()
            return True

    async def _complete(
        self, session: AsyncSession, *, job_id: UUID, token: ClaimToken
    ) -> None:
        async with session.begin():
            job = await session.scalar(
                select(Job).where(Job.id == job_id).with_for_update()
            )
            if (
                job is None
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
            version.parse_status = ParseStatus.NOT_STARTED.value
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

    @staticmethod
    async def _mark_failed(
        session: AsyncSession,
        *,
        job_id: UUID,
        token: ClaimToken,
        code: str,
        message: str,
        retryable: bool,
    ) -> bool:
        async with session.begin():
            job = await session.scalar(
                select(Job).where(Job.id == job_id).with_for_update()
            )
            if (
                job is None
                or job.status != JobStatus.RUNNING.value
                or job.attempt_count != token.attempt_number
            ):
                return False
            attempt = await session.scalar(
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
            if attempt is None:
                return False
            now = datetime.now(UTC)
            job.status = JobStatus.FAILED.value
            job.progress = 0
            job.retryable = retryable
            job.error_code = code
            job.error_message = message[:2000]
            job.finished_at = now
            job.updated_at = now
            attempt.status = JobAttemptStatus.FAILED.value
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now
            attempt.finished_at = now
            attempt.error_code = code
            attempt.error_message = message[:2000]
            await SourceIngestWorker._set_ingest_failed(
                session,
                job=job,
                code=code,
                message=message,
                now=now,
            )
            await session.flush()
            return True

    @staticmethod
    async def _set_ingest_failed(
        session: AsyncSession,
        *,
        job: Job,
        code: str,
        message: str,
        now: datetime,
    ) -> None:
        if job.source_version_id is not None:
            version = await session.scalar(
                select(SourceVersion).where(SourceVersion.id == job.source_version_id)
            )
            if version is not None:
                version.processing_status = ProcessingStatus.FAILED.value
                source = await session.scalar(
                    select(Source).where(Source.id == version.source_id)
                )
                if source is not None:
                    source.status = SourceStatus.FAILED.value
                    source.updated_at = now
        job.status = JobStatus.FAILED.value
        job.progress = 0
        job.error_code = code
        job.error_message = message[:2000]
        job.finished_at = now
        job.updated_at = now


async def recover_expired_leases(session: AsyncSession, *, batch_size: int) -> int:
    now = datetime.now(UTC)
    jobs = list(
        await session.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.RUNNING.value,
                select(JobAttempt.id)
                .where(
                    JobAttempt.job_id == Job.id,
                    JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    JobAttempt.lease_expires_at <= now,
                )
                .exists(),
            )
            .order_by(Job.updated_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    recovered = 0
    for job in jobs:
        attempt = await session.scalar(
            select(JobAttempt)
            .where(
                JobAttempt.job_id == job.id,
                JobAttempt.attempt_number == job.attempt_count,
                JobAttempt.status == JobAttemptStatus.RUNNING.value,
                JobAttempt.lease_expires_at <= now,
            )
            .with_for_update()
        )
        if attempt is None:
            continue
        attempt.status = JobAttemptStatus.FAILED.value
        attempt.finished_at = now
        attempt.error_code = "lease_expired"
        attempt.error_message = "The worker lease expired before completion."
        if job.attempt_count >= MAX_AUTOMATIC_ATTEMPTS:
            job.retryable = True
            await SourceIngestWorker._set_ingest_failed(
                session,
                job=job,
                code="automatic_attempts_exhausted",
                message="Automatic ingestion recovery attempts were exhausted.",
                now=now,
            )
            continue
        job.status = JobStatus.QUEUED.value
        job.progress = 0
        job.retryable = False
        job.error_code = None
        job.error_message = None
        job.finished_at = None
        job.updated_at = now
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=job.space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type="job.source_ingest.requested",
                deduplication_key=f"lease-recovery:{attempt.id}",
                payload={"job_id": str(job.id), "space_id": str(job.space_id)},
                available_at=now
                + timedelta(seconds=min(300, 2 ** min(job.attempt_count, 8))),
            )
        )
        recovered += 1
    await session.flush()
    return recovered


async def mark_transient_failure(
    settings: Settings,
    *,
    job_id: UUID,
    token: ClaimToken,
    message: str,
) -> None:
    engine = create_engine(settings)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await SourceIngestWorker(settings)._mark_failed(
                session,
                job_id=job_id,
                token=token,
                code="source_ingest_transient_failure",
                message=message,
                retryable=True,
            )
    finally:
        await engine.dispose()


def mark_transient_failure_sync(
    settings: Settings,
    *,
    job_id: UUID,
    token: ClaimToken,
    message: str,
) -> None:
    asyncio.run(
        mark_transient_failure(
            settings,
            job_id=job_id,
            token=token,
            message=message,
        )
    )


async def recover_orphaned_queued_jobs(
    session: AsyncSession,
    *,
    batch_size: int,
    orphan_after_seconds: int = 300,
) -> int:
    cutoff = datetime.now(UTC) - timedelta(seconds=orphan_after_seconds)
    jobs = list(
        await session.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.QUEUED.value,
                Job.updated_at <= cutoff,
                ~select(OutboxEvent.id)
                .where(
                    OutboxEvent.aggregate_id == Job.id,
                    OutboxEvent.event_type == "job.source_ingest.requested",
                    (
                        (OutboxEvent.published_at.is_(None))
                        | (OutboxEvent.published_at > cutoff)
                    ),
                )
                .exists(),
            )
            .order_by(Job.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=job.space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type="job.source_ingest.requested",
                deduplication_key=f"queued-watchdog:{job.id}:{uuid4()}",
                payload={"job_id": str(job.id), "space_id": str(job.space_id)},
            )
        )
    await session.flush()
    return len(jobs)


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
