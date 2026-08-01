from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from knowledge_workbench.application.jobs import JobService
from knowledge_workbench.db.models import Job, JobRetryRequest, JobStatus


class FakeSession:
    def __init__(self, job: Job, existing_retry: JobRetryRequest | None) -> None:
        self.values = [job, existing_retry]
        self.added: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        del statement
        return self.values.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        pass


def _failed_job() -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        space_id=uuid4(),
        kind="source_ingest",
        status=JobStatus.FAILED.value,
        progress=0,
        attempt_count=2,
        retryable=True,
        created_at=now,
        updated_at=now,
    )


async def test_retry_replay_uses_append_only_request_history() -> None:
    job = _failed_job()
    request = JobRetryRequest(
        id=uuid4(),
        job_id=job.id,
        idempotency_key="retry-a",
        target_attempt_number=1,
        created_at=datetime.now(UTC),
    )
    session = FakeSession(job, request)

    result = await JobService().retry_job(
        session,  # type: ignore[arg-type]
        space_id=job.space_id,
        job_id=job.id,
        idempotency_key="retry-a",
    )

    assert result is job
    assert job.status == JobStatus.FAILED.value
    assert session.added == []


async def test_new_retry_records_request_and_outbox() -> None:
    job = _failed_job()
    session = FakeSession(job, None)

    await JobService().retry_job(
        session,  # type: ignore[arg-type]
        space_id=job.space_id,
        job_id=job.id,
        idempotency_key="retry-b",
    )

    assert job.status == JobStatus.QUEUED.value
    assert len(session.added) == 2
    retry = session.added[0]
    assert isinstance(retry, JobRetryRequest)
    assert retry.target_attempt_number == 3
