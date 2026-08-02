from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fake_storage import FakeObjectStorage

from knowledge_workbench.application.canonical_document import (
    CanonicalBlock,
    CanonicalDocument,
)
from knowledge_workbench.application.ports.document_parser import ParseResult
from knowledge_workbench.application.ports.object_storage import StoredObject
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobAttempt,
    JobAttemptStatus,
    JobKind,
    JobStatus,
    ParseArtifactStatus,
    ParseStatus,
    Source,
    SourceParseArtifact,
    SourceStatus,
    SourceVersion,
)
from knowledge_workbench.infrastructure.parsing.fake import DeterministicFakeParser
from knowledge_workbench.worker.job_runner import ClaimToken, PermanentJobError
from knowledge_workbench.worker.source_parse import SourceParseWorker


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        del args


class _FinishSession:
    def __init__(self, *values: object | None) -> None:
        self.values = list(values)
        self.added: list[object] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    async def scalar(self, statement: object) -> object | None:
        del statement
        return self.values.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)


def _parse_state() -> tuple[Source, SourceVersion, SourceParseArtifact, ParseResult]:
    now = datetime.now(UTC)
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
        id=uuid4(),
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
        source_version_id=version.id,
        revision=1,
        parser_name="pending",
        parser_version="pending",
        parser_config={},
        status=ParseArtifactStatus.PARSING.value,
        warnings=[],
        artifact_metadata={},
        created_at=now,
    )
    result = ParseResult(
        document=CanonicalDocument(
            blocks=(
                CanonicalBlock(
                    block_id="paragraph-1",
                    ordinal=0,
                    block_type="paragraph",
                    text="Frozen evidence",
                ),
            )
        ),
        parser_name="deterministic-fake",
        parser_version="1",
        parser_config={},
        native_json=b"{}",
        markdown=b"Frozen evidence",
    )
    return source, version, artifact, result


def _stored(attempt_id: object) -> dict[str, StoredObject]:
    prefix = f"artifacts/attempts/{attempt_id}"
    return {
        "native": StoredObject(
            key=f"{prefix}/native.json",
            size=2,
            etag="native",
            media_type="application/json",
            content_sha256="a" * 64,
        ),
        "markdown": StoredObject(
            key=f"{prefix}/document.md",
            size=15,
            etag="markdown",
            media_type="text/markdown",
            content_sha256="b" * 64,
        ),
        "canonical": StoredObject(
            key=f"{prefix}/canonical.json",
            size=2,
            etag="canonical",
            media_type="application/json",
            content_sha256="c" * 64,
        ),
    }


async def test_stale_attempt_cannot_replace_winner_artifact_pointers() -> None:
    source, version, artifact, result = _parse_state()
    worker = SourceParseWorker(
        Settings(app_env="test"), DeterministicFakeParser(), FakeObjectStorage()
    )
    now = datetime.now(UTC)
    job = Job(
        id=uuid4(),
        space_id=source.space_id,
        source_version_id=version.id,
        kind=JobKind.SOURCE_PARSE.value,
        status=JobStatus.RUNNING.value,
        progress=20,
        attempt_count=2,
        retryable=False,
        created_at=now,
        updated_at=now,
    )
    stale_token = ClaimToken(uuid4(), 1)
    winner_token = ClaimToken(uuid4(), 2)
    stale_keys = _stored(stale_token.attempt_id)
    winner_keys = _stored(winner_token.attempt_id)

    stale = await worker._finish(  # noqa: SLF001
        _FinishSession(job),  # type: ignore[arg-type]
        job_id=job.id,
        token=stale_token,
        source=source,
        version=version,
        artifact=artifact,
        result=result,
        keys=stale_keys,
    )

    assert stale is False
    assert artifact.native_storage_key is None

    attempt = JobAttempt(
        id=winner_token.attempt_id,
        job_id=job.id,
        attempt_number=winner_token.attempt_number,
        status=JobAttemptStatus.RUNNING.value,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=5),
    )
    winner_session = _FinishSession(job, attempt, artifact, version, None)
    won = await worker._finish(  # noqa: SLF001
        winner_session,  # type: ignore[arg-type]
        job_id=job.id,
        token=winner_token,
        source=source,
        version=version,
        artifact=artifact,
        result=result,
        keys=winner_keys,
    )

    assert won is True
    assert artifact.native_storage_key == winner_keys["native"].key
    assert artifact.markdown_storage_key == winner_keys["markdown"].key
    assert artifact.canonical_storage_key == winner_keys["canonical"].key
    assert version.current_parse_artifact_id == artifact.id
    assert job.status == JobStatus.SUCCEEDED.value
    assert attempt.status == JobAttemptStatus.SUCCEEDED.value
    assert len(winner_session.added) == 1


class _OverflowStorage(FakeObjectStorage):
    def __init__(self) -> None:
        super().__init__()
        self.created_temp_path: Path | None = None

    async def iter_bytes(self, key: str) -> AsyncIterator[bytes]:
        del key
        yield b"123456"


async def test_download_removes_partial_temp_file_when_stream_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _OverflowStorage()
    storage.put("source.txt", b"12345", "text/plain")
    version = SourceVersion(
        id=uuid4(),
        source_id=uuid4(),
        version_number=1,
        storage_key="source.txt",
        content_sha256="0" * 64,
        acquisition_type="upload",
        acquisition_metadata={},
        processing_status="ready",
        parse_status=ParseStatus.PARSING.value,
    )
    worker = SourceParseWorker(
        Settings(app_env="test", parse_max_source_bytes=5),
        DeterministicFakeParser(),
        storage,
    )
    original = tempfile.NamedTemporaryFile

    def tracking_temp_file(*args: object, **kwargs: object) -> Any:
        handle = original(*args, **kwargs)
        storage.created_temp_path = Path(handle.name)
        return handle

    monkeypatch.setattr(
        "knowledge_workbench.worker.source_parse.tempfile.NamedTemporaryFile",
        tracking_temp_file,
    )

    with pytest.raises(PermanentJobError, match="Source exceeds parser size limit"):
        await worker._download(version, ".txt")  # noqa: SLF001

    assert storage.created_temp_path is not None
    assert not storage.created_temp_path.exists()


async def test_artifact_keys_are_attempt_scoped_and_stale_objects_are_deleted() -> None:
    source, version, artifact, result = _parse_state()
    storage = FakeObjectStorage()
    worker = SourceParseWorker(
        Settings(app_env="test"), DeterministicFakeParser(), storage
    )
    first_token = ClaimToken(uuid4(), 1)
    second_token = ClaimToken(uuid4(), 2)

    first = await worker._write_artifacts(  # noqa: SLF001
        source, version, artifact, result, token=first_token
    )
    second = await worker._write_artifacts(  # noqa: SLF001
        source, version, artifact, result, token=second_token
    )

    assert set(stored.key for stored in first.values()).isdisjoint(
        stored.key for stored in second.values()
    )
    assert all(f"/attempts/{first_token.attempt_id}/" in value.key for value in first.values())
    assert all(
        f"/attempts/{second_token.attempt_id}/" in value.key for value in second.values()
    )

    await worker._delete_artifacts(first)  # noqa: SLF001

    assert all(value.key not in storage.objects for value in first.values())
    assert all(value.key in storage.objects for value in second.values())
