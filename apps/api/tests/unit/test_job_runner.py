from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobKind,
    JobStatus,
    ParseArtifactStatus,
    ParseStatus,
    Source,
    SourceParseArtifact,
    SourceStatus,
    SourceVersion,
)
from knowledge_workbench.worker.job_runner import (
    JobRunner,
    UnsupportedJobKindError,
    _attempts_in_current_budget,
    _set_exhausted_failure,
    requested_event_type,
)


def test_requested_event_type_is_explicit_by_job_kind() -> None:
    assert requested_event_type(JobKind.SOURCE_INGEST) == "job.source_ingest.requested"
    assert requested_event_type(JobKind.SOURCE_PARSE) == "job.source_parse.requested"
    assert requested_event_type(JobKind.SOURCE_EXTRACT) == "job.source_extract.requested"


@pytest.mark.parametrize("job_kind", [JobKind.SOURCE_INDEX, "unknown"])
def test_requested_event_type_fails_closed(job_kind: JobKind | str) -> None:
    with pytest.raises(UnsupportedJobKindError):
        requested_event_type(job_kind)


def test_runner_uses_parse_specific_lease() -> None:
    settings = Settings(
        app_env="test",
        job_attempt_lease_seconds=30,
        parse_attempt_lease_seconds=900,
    )

    assert JobRunner(settings, JobKind.SOURCE_INGEST)._lease_seconds() == 30
    assert JobRunner(settings, JobKind.SOURCE_PARSE)._lease_seconds() == 900


def test_attempt_budget_is_scoped_to_manual_retry() -> None:
    job = Job(attempt_count=11, attempt_budget_start=8)

    assert _attempts_in_current_budget(job) == 3


async def test_runner_does_not_claim_a_different_job_kind() -> None:
    class Session:
        async def scalar(self, statement: object) -> object:
            del statement
            return type(
                "FakeJob",
                (),
                {
                    "id": uuid4(),
                    "kind": JobKind.SOURCE_PARSE.value,
                    "status": "queued",
                },
            )()

        def begin(self) -> Any:
            class Transaction:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *args: object) -> None:
                    del args

            return Transaction()

    token = await JobRunner(Settings(app_env="test"), JobKind.SOURCE_INGEST)._claim(
        Session(),  # type: ignore[arg-type]
        job_id=uuid4(),
        celery_task_id=None,
        worker_name=None,
    )

    assert token is None


async def test_parse_exhaustion_fails_revision_but_keeps_source_active() -> None:
    now = datetime.now(UTC)
    version_id = uuid4()
    source = Source(
        id=uuid4(),
        space_id=uuid4(),
        kind="markdown",
        title="Source",
        status=SourceStatus.ACTIVE.value,
        created_at=now,
        updated_at=now,
    )
    version = SourceVersion(
        id=version_id,
        source_id=source.id,
        version_number=1,
        acquisition_type="upload",
        acquisition_metadata={},
        processing_status="ready",
        parse_status=ParseStatus.PARSING.value,
        created_at=now,
    )
    artifact = SourceParseArtifact(
        id=uuid4(),
        source_version_id=version_id,
        revision=1,
        parser_name="docling",
        parser_version="2.117.0",
        parser_config={},
        status=ParseArtifactStatus.PARSING.value,
        warnings=[],
        artifact_metadata={},
        created_at=now,
    )
    job = Job(
        id=uuid4(),
        space_id=source.space_id,
        source_version_id=version_id,
        kind=JobKind.SOURCE_PARSE.value,
        status=JobStatus.RUNNING.value,
        progress=20,
        attempt_count=8,
        retryable=False,
        created_at=now,
        updated_at=now,
    )

    class Session:
        def __init__(self) -> None:
            self.values: list[object] = [version, artifact]

        async def scalar(self, statement: object) -> object:
            del statement
            return self.values.pop(0)

    await _set_exhausted_failure(Session(), job=job, now=now)  # type: ignore[arg-type]

    assert job.status == JobStatus.FAILED.value
    assert job.retryable is True
    assert version.parse_status == ParseStatus.FAILED.value
    assert artifact.status == ParseArtifactStatus.FAILED.value
    assert artifact.error_code == "automatic_attempts_exhausted"
    assert source.status == SourceStatus.ACTIVE.value
