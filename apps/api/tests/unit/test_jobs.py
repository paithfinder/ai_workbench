from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from knowledge_workbench.application.jobs import JobService
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    Job,
    JobRetryRequest,
    JobStatus,
    OutboxEvent,
    ParseArtifactStatus,
    ParseStatus,
    SourceParseArtifact,
    SourceVersion,
)


class FakeSession:
    def __init__(self, *values: object | None) -> None:
        self.values = list(values)
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
    event = session.added[1]
    assert isinstance(event, OutboxEvent)
    assert event.event_type == "job.source_ingest.requested"


async def test_parse_retry_resets_failed_revision_and_uses_parse_event() -> None:
    job = _failed_job()
    job.kind = "source_parse"
    job.source_version_id = uuid4()
    version = SourceVersion(
        id=job.source_version_id,
        source_id=uuid4(),
        version_number=1,
        acquisition_type="upload",
        acquisition_metadata={},
        processing_status="ready",
        parse_status=ParseStatus.FAILED.value,
    )
    artifact = SourceParseArtifact(
        id=uuid4(),
        source_version_id=job.source_version_id,
        revision=2,
        parser_name="docling",
        parser_version="2.117.0",
        parser_config={},
        status=ParseArtifactStatus.FAILED.value,
        error_code="parser_dependency_unavailable",
        error_message="temporary failure",
        warnings=[],
        artifact_metadata={},
    )
    session = FakeSession(job, None, artifact, version)

    await JobService().retry_job(
        session,  # type: ignore[arg-type]
        space_id=job.space_id,
        job_id=job.id,
        idempotency_key="retry-parse",
    )

    assert job.attempt_budget_start == job.attempt_count
    assert artifact.status == ParseArtifactStatus.QUEUED.value
    assert artifact.error_code is None
    assert artifact.error_message is None
    assert version.parse_status == ParseStatus.QUEUED.value
    event = session.added[1]
    assert isinstance(event, OutboxEvent)
    assert event.event_type == "job.source_parse.requested"


async def test_retry_fails_closed_for_job_without_worker_route() -> None:
    job = _failed_job()
    job.kind = "source_extract"
    session = FakeSession(job, None)

    with pytest.raises(AppError) as error:
        await JobService().retry_job(
            session,  # type: ignore[arg-type]
            space_id=job.space_id,
            job_id=job.id,
            idempotency_key="retry-extract",
        )

    assert error.value.code == "job_kind_not_retryable"
    assert job.status == JobStatus.FAILED.value
    assert session.added == []
