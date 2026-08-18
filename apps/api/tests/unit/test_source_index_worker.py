from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from knowledge_workbench.application.ports.embedding_gateway import EmbeddingResult
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobAttempt,
    JobAttemptStatus,
    JobKind,
    JobStatus,
    KnowledgeNode,
    KnowledgeRevision,
    RetrievalIndexRun,
)
from knowledge_workbench.worker.job_runner import ClaimToken, PermanentJobError
from knowledge_workbench.worker.source_index import SourceIndexWorker


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        del args


class _Session:
    def __init__(self, *values: object | None, scope_node: KnowledgeNode) -> None:
        self._values = list(values)
        self.scope_node = scope_node
        self.executed: list[object] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    async def scalar(self, statement: object) -> object | None:
        del statement
        return self._values.pop(0)

    async def get(self, model: object, identity: object) -> KnowledgeNode:
        del model, identity
        return self.scope_node

    async def execute(self, statement: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.executed.append(statement)


class _EmbeddingGateway:
    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=[[0.0] * 1024 for _ in texts],
            provider="fake",
            model="fake-bge-m3",
            dimensions=1024,
        )


async def test_stale_knowledge_revision_does_not_deactivate_current_chunks() -> None:
    now = datetime.now(UTC)
    space_id = uuid4()
    node_id = uuid4()
    stale_revision_id = uuid4()
    job = Job(
        id=uuid4(),
        space_id=space_id,
        kind=JobKind.SOURCE_INDEX.value,
        status=JobStatus.RUNNING.value,
        progress=25,
        attempt_count=1,
        retryable=False,
        created_at=now,
        updated_at=now,
    )
    run = RetrievalIndexRun(
        id=uuid4(),
        job_id=job.id,
        space_id=space_id,
        target_kind="knowledge_revision",
        target_id=stale_revision_id,
        scope_node_id=node_id,
        input_hash="a" * 64,
        knowledge_revision_id=stale_revision_id,
        status="running",
        index_config_version="d7-v1",
        chunker_version="d7-structured-v1",
        embedding_provider="fake",
        embedding_model="fake-bge-m3",
        embedding_config={"dimensions": 1024},
        embedding_dimensions=1024,
    )
    revision = KnowledgeRevision(
        id=stale_revision_id,
        node_id=node_id,
        space_id=space_id,
        revision_number=1,
        title="Stale knowledge",
        body="Old content",
        tags=[],
        conditions=[],
        exceptions=[],
        actor="test",
        content_hash="a" * 64,
    )
    token = ClaimToken(uuid4(), 1)
    attempt = JobAttempt(
        id=token.attempt_id,
        job_id=job.id,
        attempt_number=token.attempt_number,
        status=JobAttemptStatus.RUNNING.value,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=5),
    )
    session = _Session(
        job,
        run,
        revision,
        job,
        run,
        attempt,
        None,
        scope_node=KnowledgeNode(
            id=node_id,
            space_id=space_id,
            kind="document",
            path=f"n{node_id.hex}",
            version=2,
            sort_order=0,
            current_revision_id=uuid4(),
        ),
    )
    worker = SourceIndexWorker(Settings(app_env="test"), _EmbeddingGateway())

    with pytest.raises(PermanentJobError, match="no longer current"):
        await worker._run_claimed(session, job_id=job.id, token=token)  # noqa: SLF001

    assert session.executed == []
