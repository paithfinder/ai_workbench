from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    ExtractionJob,
    ExtractionStatus,
    Job,
    JobAttempt,
    JobAttemptStatus,
    JobKind,
    JobStatus,
    OutboxEvent,
    ParseArtifactStatus,
    ParseStatus,
    ProcessingStatus,
    RetrievalIndexRun,
    Source,
    SourceParseArtifact,
    SourceStatus,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine, create_session_factory

MAX_AUTOMATIC_ATTEMPTS = 8
RELIABLE_JOB_KINDS = (
    JobKind.SOURCE_INGEST,
    JobKind.SOURCE_PARSE,
    JobKind.SOURCE_EXTRACT,
    JobKind.SOURCE_INDEX,
)

_REQUESTED_EVENT_TYPES = {
    JobKind.SOURCE_INGEST: "job.source_ingest.requested",
    JobKind.SOURCE_PARSE: "job.source_parse.requested",
    JobKind.SOURCE_EXTRACT: "job.source_extract.requested",
    JobKind.SOURCE_INDEX: "job.source_index.requested",
}


class UnsupportedJobKindError(ValueError):
    pass


class ActiveJobLeaseError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("Another worker owns the active job lease")


class PermanentJobError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ClaimToken:
    def __init__(self, attempt_id: UUID, attempt_number: int) -> None:
        self.attempt_id = attempt_id
        self.attempt_number = attempt_number


class JobAttemptError(Exception):
    def __init__(self, token: ClaimToken, message: str) -> None:
        self.token = token
        super().__init__(message)


def requested_event_type(job_kind: JobKind | str) -> str:
    """Return the dispatch event for a worker-backed job kind, rejecting unknown kinds."""
    try:
        kind = job_kind if isinstance(job_kind, JobKind) else JobKind(job_kind)
        return _REQUESTED_EVENT_TYPES[kind]
    except (KeyError, ValueError) as exc:
        raise UnsupportedJobKindError(f"Unsupported worker job kind: {job_kind}") from exc


class JobRunner:
    """Shared transactional ownership and lease lifecycle for a single job kind."""

    def __init__(self, settings: Settings, job_kind: JobKind) -> None:
        requested_event_type(job_kind)
        self._settings = settings
        self._job_kind = job_kind

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
                await self._run_claimed(session, job_id=job_id, token=token)
            except PermanentJobError:
                raise
            except Exception as exc:
                raise JobAttemptError(token, str(exc)) from exc
        except ActiveJobLeaseError:
            raise
        except PermanentJobError as exc:
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
        except JobAttemptError:
            await session.rollback()
            raise
        except Exception:
            await session.rollback()
            raise

    async def _run_claimed(self, session: AsyncSession, *, job_id: UUID, token: ClaimToken) -> None:
        if not await self._heartbeat(session, job_id=job_id, token=token):
            return
        await self._complete(session, job_id=job_id, token=token)

    def _lease_seconds(self) -> int:
        if self._job_kind == JobKind.SOURCE_PARSE:
            return self._settings.parse_attempt_lease_seconds
        return self._settings.job_attempt_lease_seconds

    async def _claim(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        celery_task_id: str | None,
        worker_name: str | None,
    ) -> ClaimToken | None:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if (
                job is None
                or job.kind != self._job_kind.value
                or job.status == JobStatus.SUCCEEDED.value
            ):
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
                if _attempts_in_current_budget(job) >= MAX_AUTOMATIC_ATTEMPTS:
                    job.retryable = True
                    await _set_exhausted_failure(session, job=job, now=now)
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
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds()),
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
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if (
                job is None
                or job.kind != self._job_kind.value
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
            attempt.lease_expires_at = now + timedelta(seconds=self._lease_seconds())
            job.updated_at = now
            await session.flush()
            return True

    async def _complete(self, session: AsyncSession, *, job_id: UUID, token: ClaimToken) -> None:
        raise NotImplementedError

    async def _mark_failed(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        token: ClaimToken,
        code: str,
        message: str,
        retryable: bool,
    ) -> bool:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if (
                job is None
                or job.kind != self._job_kind.value
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
            _set_job_failed(job, code=code, message=message, retryable=retryable, now=now)
            attempt.status = JobAttemptStatus.FAILED.value
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now
            attempt.finished_at = now
            attempt.error_code = code
            attempt.error_message = message[:2000]
            await self._on_failed(session, job=job, now=now)
            await session.flush()
            return True

    async def _release_for_retry(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        token: ClaimToken,
        code: str,
        message: str,
    ) -> bool:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if (
                job is None
                or job.kind != self._job_kind.value
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
            attempt.status = JobAttemptStatus.FAILED.value
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now
            attempt.finished_at = now
            attempt.error_code = code
            attempt.error_message = message[:2000]
            if _attempts_in_current_budget(job) >= MAX_AUTOMATIC_ATTEMPTS:
                job.retryable = True
                await _set_exhausted_failure(session, job=job, now=now)
            else:
                job.status = JobStatus.QUEUED.value
                job.progress = 0
                job.retryable = False
                job.error_code = None
                job.error_message = None
                job.finished_at = None
                job.updated_at = now
            await session.flush()
            return True

    async def _on_failed(self, session: AsyncSession, *, job: Job, now: datetime) -> None:
        await _set_kind_failure_state(session, job=job, now=now)


async def recover_expired_leases(
    session: AsyncSession,
    *,
    batch_size: int,
    job_kinds: Collection[JobKind] = RELIABLE_JOB_KINDS,
) -> int:
    kinds = _validated_kinds(job_kinds)
    now = datetime.now(UTC)
    jobs = list(
        await session.scalars(
            select(Job)
            .where(
                Job.kind.in_([kind.value for kind in kinds]),
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
        if _attempts_in_current_budget(job) >= MAX_AUTOMATIC_ATTEMPTS:
            job.retryable = True
            await _set_exhausted_failure(session, job=job, now=now)
            continue
        job.status = JobStatus.QUEUED.value
        job.progress = 0
        job.retryable = False
        job.error_code = None
        job.error_message = None
        job.finished_at = None
        job.updated_at = now
        event_type = requested_event_type(job.kind)
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=job.space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type=event_type,
                deduplication_key=f"lease-recovery:{attempt.id}",
                payload={"job_id": str(job.id), "space_id": str(job.space_id)},
                available_at=now + timedelta(seconds=min(300, 2 ** min(job.attempt_count, 8))),
            )
        )
        recovered += 1
    await session.flush()
    return recovered


async def recover_orphaned_queued_jobs(
    session: AsyncSession,
    *,
    batch_size: int,
    orphan_after_seconds: int = 300,
    job_kinds: Collection[JobKind] = RELIABLE_JOB_KINDS,
) -> int:
    kinds = _validated_kinds(job_kinds)
    cutoff = datetime.now(UTC) - timedelta(seconds=orphan_after_seconds)
    event_types = [requested_event_type(kind) for kind in kinds]
    jobs = list(
        await session.scalars(
            select(Job)
            .where(
                Job.kind.in_([kind.value for kind in kinds]),
                Job.status == JobStatus.QUEUED.value,
                Job.updated_at <= cutoff,
                ~select(OutboxEvent.id)
                .where(
                    OutboxEvent.aggregate_id == Job.id,
                    OutboxEvent.event_type.in_(event_types),
                    (OutboxEvent.published_at.is_(None)) | (OutboxEvent.published_at > cutoff),
                )
                .exists(),
            )
            .order_by(Job.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        event_type = requested_event_type(job.kind)
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=job.space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type=event_type,
                deduplication_key=f"queued-watchdog:{job.id}:{uuid4()}",
                payload={"job_id": str(job.id), "space_id": str(job.space_id)},
            )
        )
    await session.flush()
    return len(jobs)


async def release_transient_attempt(
    settings: Settings,
    *,
    job_kind: JobKind,
    job_id: UUID,
    token: ClaimToken,
    message: str,
    error_code: str | None = None,
) -> None:
    engine = create_engine(settings)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    try:
        async with session_factory() as session:
            runner = JobRunner(settings, job_kind)
            await runner._release_for_retry(
                session,
                job_id=job_id,
                token=token,
                code=error_code or f"{job_kind.value}_transient_failure",
                message=message,
            )
    finally:
        await engine.dispose()


def release_transient_attempt_sync(
    settings: Settings,
    *,
    job_kind: JobKind,
    job_id: UUID,
    token: ClaimToken,
    message: str,
    error_code: str | None = None,
) -> None:
    asyncio.run(
        release_transient_attempt(
            settings,
            job_kind=job_kind,
            job_id=job_id,
            token=token,
            message=message,
            error_code=error_code,
        )
    )


async def mark_transient_failure(
    settings: Settings,
    *,
    job_kind: JobKind,
    job_id: UUID,
    token: ClaimToken,
    message: str,
    error_code: str | None = None,
) -> None:
    engine = create_engine(settings)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    try:
        async with session_factory() as session:
            runner = JobRunner(settings, job_kind)
            await runner._mark_failed(
                session,
                job_id=job_id,
                token=token,
                code=error_code or f"{job_kind.value}_transient_failure",
                message=message,
                retryable=True,
            )
    finally:
        await engine.dispose()


def mark_transient_failure_sync(
    settings: Settings,
    *,
    job_kind: JobKind,
    job_id: UUID,
    token: ClaimToken,
    message: str,
    error_code: str | None = None,
) -> None:
    asyncio.run(
        mark_transient_failure(
            settings,
            job_kind=job_kind,
            job_id=job_id,
            token=token,
            message=message,
            error_code=error_code,
        )
    )


def _attempts_in_current_budget(job: Job) -> int:
    return job.attempt_count - (job.attempt_budget_start or 0)


def _validated_kinds(job_kinds: Collection[JobKind]) -> tuple[JobKind, ...]:
    kinds = tuple(job_kinds)
    if not kinds:
        raise ValueError("At least one job kind is required")
    for kind in kinds:
        requested_event_type(kind)
    return kinds


def _set_job_failed(job: Job, *, code: str, message: str, retryable: bool, now: datetime) -> None:
    job.status = JobStatus.FAILED.value
    job.progress = 0
    job.retryable = retryable
    job.error_code = code
    job.error_message = message[:2000]
    job.finished_at = now
    job.updated_at = now


async def _set_exhausted_failure(
    session: AsyncSession,
    *,
    job: Job,
    now: datetime,
) -> None:
    _set_job_failed(
        job,
        code="automatic_attempts_exhausted",
        message="Automatic job recovery attempts were exhausted.",
        retryable=True,
        now=now,
    )
    await _set_kind_failure_state(session, job=job, now=now)


async def _set_kind_failure_state(
    session: AsyncSession,
    *,
    job: Job,
    now: datetime,
) -> None:
    if job.kind == JobKind.SOURCE_INGEST.value and job.source_version_id is not None:
        version = await session.scalar(
            select(SourceVersion).where(SourceVersion.id == job.source_version_id)
        )
        if version is not None:
            version.processing_status = ProcessingStatus.FAILED.value
            source = await session.scalar(select(Source).where(Source.id == version.source_id))
            if source is not None:
                source.status = SourceStatus.FAILED.value
                source.updated_at = now
    elif job.kind == JobKind.SOURCE_PARSE.value and job.source_version_id is not None:
        version = await session.scalar(
            select(SourceVersion).where(SourceVersion.id == job.source_version_id)
        )
        artifact = await session.scalar(
            select(SourceParseArtifact)
            .where(
                SourceParseArtifact.source_version_id == job.source_version_id,
                SourceParseArtifact.status.in_(
                    [ParseArtifactStatus.QUEUED.value, ParseArtifactStatus.PARSING.value]
                ),
            )
            .order_by(SourceParseArtifact.revision.desc())
        )
        if version is not None:
            version.parse_status = ParseStatus.FAILED.value
        if artifact is not None:
            artifact.status = ParseArtifactStatus.FAILED.value
            artifact.error_code = job.error_code
            artifact.error_message = job.error_message
            artifact.completed_at = now
    elif job.kind == JobKind.SOURCE_INDEX.value:
        index_run = await session.scalar(
            select(RetrievalIndexRun).where(RetrievalIndexRun.job_id == job.id)
        )
        if index_run is not None:
            index_run.status = "failed"
            index_run.error_code = job.error_code
            index_run.error_message = job.error_message
            index_run.completed_at = now
    elif job.kind == JobKind.SOURCE_EXTRACT.value:
        extraction = await session.scalar(
            select(ExtractionJob).where(ExtractionJob.job_id == job.id)
        )
        if extraction is not None:
            extraction.status = ExtractionStatus.FAILED.value
            extraction.error_code = job.error_code
            extraction.error_message = job.error_message
            extraction.completed_at = now
