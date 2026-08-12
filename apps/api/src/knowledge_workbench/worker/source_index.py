from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_workbench.application.chunking import TextChunkInput, chunk_inputs, chunk_sections
from knowledge_workbench.application.ports.embedding_gateway import (
    EmbeddingGateway,
    EmbeddingInvalidResponseError,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobAttempt,
    JobAttemptStatus,
    JobKind,
    JobStatus,
    KnowledgeNode,
    KnowledgeRevision,
    RetrievalChunk,
    RetrievalIndexRun,
    SourceSection,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine, create_session_factory
from knowledge_workbench.infrastructure.ai.bge_m3 import BGEM3HttpEmbeddingGateway
from knowledge_workbench.infrastructure.ai.fake_embedding import FakeEmbeddingGateway
from knowledge_workbench.worker.job_runner import ClaimToken, JobRunner, PermanentJobError
from knowledge_workbench.worker.job_runner import mark_transient_failure as mark_job_failure
from knowledge_workbench.worker.job_runner import (
    mark_transient_failure_sync as mark_job_failure_sync,
)


class SourceIndexWorker(JobRunner):
    def __init__(
        self,
        settings: Settings,
        gateway: EmbeddingGateway,
        heartbeat_session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        super().__init__(settings, JobKind.SOURCE_INDEX)
        self._gateway = gateway
        self._heartbeat_sessions = heartbeat_session_factory

    async def _run_claimed(self, session: AsyncSession, *, job_id: UUID, token: ClaimToken) -> None:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not self._owns(job, token):
                return
            assert job is not None
            run = await session.scalar(
                select(RetrievalIndexRun)
                .where(RetrievalIndexRun.job_id == job_id)
                .with_for_update()
            )
            if run is None or run.scope_node_id is None:
                raise PermanentJobError("index_run_invalid", "Index run is inconsistent.")
            scope_node = await session.get(KnowledgeNode, run.scope_node_id)
            if scope_node is None or scope_node.deleted_at is not None:
                raise PermanentJobError("index_scope_missing", "Index scope node is unavailable.")
            sections: list[SourceSection] = []
            revision: KnowledgeRevision | None = None
            if run.target_kind == "source_version":
                if run.source_version_id is None or run.parse_artifact_id is None:
                    raise PermanentJobError("index_run_invalid", "Source target is inconsistent.")
                version = await session.scalar(
                    select(SourceVersion).where(SourceVersion.id == run.source_version_id)
                )
                if (
                    version is None
                    or version.current_parse_artifact_id != run.parse_artifact_id
                ):
                    raise PermanentJobError(
                        "index_input_stale", "Source parse artifact is no longer current."
                    )
                sections = list(
                    await session.scalars(
                        select(SourceSection)
                        .where(
                            SourceSection.parse_artifact_id == run.parse_artifact_id,
                            SourceSection.source_version_id == run.source_version_id,
                            SourceSection.space_id == run.space_id,
                        )
                        .order_by(SourceSection.ordinal)
                    )
                )
                if not sections:
                    raise PermanentJobError(
                        "index_input_missing", "Parsed sections are unavailable."
                    )
            elif run.target_kind == "knowledge_revision":
                revision = await session.scalar(
                    select(KnowledgeRevision)
                    .join(KnowledgeNode)
                    .where(
                        KnowledgeRevision.id == run.knowledge_revision_id,
                        KnowledgeNode.id == scope_node.id,
                        KnowledgeNode.current_revision_id == KnowledgeRevision.id,
                        KnowledgeNode.deleted_at.is_(None),
                    )
                )
                if revision is None:
                    raise PermanentJobError(
                        "index_input_missing", "Current knowledge revision is unavailable."
                    )
            else:
                raise PermanentJobError("index_target_unsupported", "Index target is unsupported.")
            run.status = "running"
            run.started_at = run.started_at or datetime.now(UTC)
            run.error_code = None
            run.error_message = None
            job.progress = 25
            snapshot: dict[str, Any] = {
                "run_id": run.id,
                "space_id": run.space_id,
                "version_id": run.source_version_id,
                "artifact_id": run.parse_artifact_id,
                "revision_id": run.knowledge_revision_id,
                "node_id": scope_node.id,
                "source_id": scope_node.source_id,
                "path": scope_node.path,
                "config_version": run.index_config_version,
                "target_kind": run.target_kind,
            }

        if revision is None:
            target_identity = snapshot["source_id"] or snapshot["version_id"]
            drafts = chunk_sections(
                sections,
                target_id=target_identity,
                target_characters=self._settings.chunk_target_characters,
                overlap_characters=self._settings.chunk_overlap_characters,
                chunker_version=self._settings.chunker_version,
                index_config_version=self._settings.index_version,
            )
        else:
            canonical = "\n".join(
                [
                    revision.title,
                    revision.body,
                    "标签：" + "、".join(revision.tags) if revision.tags else "",
                    "条件：" + "；".join(revision.conditions) if revision.conditions else "",
                    "例外：" + "；".join(revision.exceptions) if revision.exceptions else "",
                ]
            )
            drafts = chunk_inputs(
                [
                    TextChunkInput(
                        target_id=revision.id,
                        section_id=revision.id,
                        text=canonical,
                        heading_path=[revision.title],
                        locator={},
                    )
                ],
                target_characters=self._settings.chunk_target_characters,
                overlap_characters=self._settings.chunk_overlap_characters,
                chunker_version=self._settings.chunker_version,
                index_config_version=self._settings.index_version,
            )
        if not drafts:
            raise PermanentJobError("index_input_empty", "No indexable content was found.")
        stop = asyncio.Event()
        heartbeat = None
        if self._heartbeat_sessions is not None:
            heartbeat = asyncio.create_task(self._heartbeat_loop(job_id, token, stop))
        try:
            result = await self._gateway.embed([draft.text for draft in drafts])
        except EmbeddingInvalidResponseError as exc:
            raise PermanentJobError("embedding_invalid_response", str(exc)) from exc
        finally:
            stop.set()
            if heartbeat is not None:
                with suppress(asyncio.CancelledError):
                    await heartbeat

        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            run = await session.scalar(
                select(RetrievalIndexRun)
                .where(RetrievalIndexRun.id == snapshot["run_id"])
                .with_for_update()
            )
            if not self._owns(job, token) or run is None:
                return
            attempt = await session.scalar(
                select(JobAttempt)
                .where(
                    JobAttempt.id == token.attempt_id,
                    JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    JobAttempt.lease_expires_at > datetime.now(UTC),
                )
                .with_for_update()
            )
            if attempt is None:
                return
            await session.execute(
                delete(RetrievalChunk).where(RetrievalChunk.index_run_id == run.id)
            )
            section_map = {section.id: section for section in sections}
            section_source_id = None
            if snapshot["target_kind"] == "source_version":
                source_row = await session.execute(
                    text("SELECT source_id FROM source_versions WHERE id=:version_id"),
                    {"version_id": snapshot["version_id"]},
                )
                section_source_id = source_row.scalar_one_or_none()
                if section_source_id is None:
                    raise PermanentJobError("source_missing", "Source is unavailable.")
            await session.execute(
                text(
                    "UPDATE retrieval_chunks SET active=false WHERE space_id=:space_id "
                    "AND ((:target_kind='source_version' AND source_id=:source_id) "
                    "OR (:target_kind='knowledge_revision' AND knowledge_node_id=:node_id))"
                ),
                {
                    "space_id": snapshot["space_id"],
                    "target_kind": snapshot["target_kind"],
                    "source_id": section_source_id,
                    "node_id": snapshot["node_id"],
                },
            )
            for draft, vector in zip(drafts, result.vectors, strict=True):
                source_mode = snapshot["target_kind"] == "source_version"
                section = section_map.get(draft.section_id)
                if source_mode and section is None:
                    raise PermanentJobError(
                        "source_section_missing",
                        "Source evidence section is unavailable.",
                    )
                session.add(
                    RetrievalChunk(
                        id=uuid4(),
                        index_run_id=run.id,
                        space_id=snapshot["space_id"],
                        corpus_kind="source_evidence" if source_mode else "confirmed_knowledge",
                        index_config_version=snapshot["config_version"],
                        active=True,
                        knowledge_node_id=None if source_mode else snapshot["node_id"],
                        knowledge_revision_id=None if source_mode else snapshot["revision_id"],
                        source_id=section_source_id if source_mode else None,
                        source_version_id=snapshot["version_id"] if source_mode else None,
                        parse_artifact_id=snapshot["artifact_id"] if source_mode else None,
                        section_id=section.id if section is not None else None,
                        ordinal=draft.ordinal,
                        content_identity=draft.content_identity,
                        content_hash=draft.content_hash,
                        title=" / ".join(draft.heading_path) or None,
                        text=draft.text,
                        char_count=len(draft.text),
                        token_count=max(1, len(draft.text) // 3),
                        path=snapshot["path"],
                        heading_path=draft.heading_path,
                        locator=draft.locator if source_mode else None,
                        embedding_model=result.model,
                        embedding_config={
                            "provider": result.provider,
                            "dimensions": result.dimensions,
                        },
                        embedding=vector,
                    )
                )
            now = datetime.now(UTC)
            run.status = "ready"
            run.chunk_count = len(drafts)
            run.embedded_count = len(drafts)
            run.completed_at = now
            run.error_code = None
            run.error_message = None
            assert job is not None
            job.status = JobStatus.SUCCEEDED.value
            job.progress = 100
            job.finished_at = now
            job.updated_at = now
            attempt.status = JobAttemptStatus.SUCCEEDED.value
            attempt.finished_at = now
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now

    async def _on_failed(self, session: AsyncSession, *, job: Job, now: datetime) -> None:
        run = await session.scalar(
            select(RetrievalIndexRun).where(RetrievalIndexRun.job_id == job.id)
        )
        if run is not None:
            run.status = "failed"
            run.error_code = job.error_code
            run.error_message = job.error_message
            run.completed_at = now

    async def _heartbeat_loop(
        self, job_id: UUID, token: ClaimToken, stop: asyncio.Event
    ) -> None:
        assert self._heartbeat_sessions is not None
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self._settings.index_heartbeat_seconds
                )
                return
            except TimeoutError:
                async with self._heartbeat_sessions() as heartbeat_session:
                    if not await self._heartbeat(
                        heartbeat_session, job_id=job_id, token=token
                    ):
                        return

    @staticmethod
    def _owns(job: Job | None, token: ClaimToken) -> bool:
        return bool(
            job is not None
            and job.kind == JobKind.SOURCE_INDEX.value
            and job.status == JobStatus.RUNNING.value
            and job.attempt_count == token.attempt_number
        )


def create_embedding_gateway(settings: Settings) -> EmbeddingGateway:
    if settings.embedding_provider == "bge_m3_http":
        return BGEM3HttpEmbeddingGateway(
            url=settings.embedding_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    return FakeEmbeddingGateway(
        model=settings.embedding_model, dimensions=settings.embedding_dimensions
    )


async def run_source_index(
    settings: Settings, *, job_id: UUID, celery_task_id: str | None, worker_name: str | None
) -> None:
    engine = create_engine(settings)
    sessions: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    try:
        async with sessions() as session:
            await SourceIndexWorker(
                settings,
                create_embedding_gateway(settings),
                heartbeat_session_factory=sessions,
            ).run(
                session, job_id=job_id, celery_task_id=celery_task_id, worker_name=worker_name
            )
    finally:
        await engine.dispose()


def run_source_index_sync(
    settings: Settings, *, job_id: UUID, celery_task_id: str | None, worker_name: str | None
) -> None:
    asyncio.run(
        run_source_index(
            settings, job_id=job_id, celery_task_id=celery_task_id, worker_name=worker_name
        )
    )


async def mark_transient_failure(
    settings: Settings, *, job_id: UUID, token: ClaimToken, message: str
) -> None:
    await mark_job_failure(
        settings, job_kind=JobKind.SOURCE_INDEX, job_id=job_id, token=token, message=message
    )


def mark_transient_failure_sync(
    settings: Settings, *, job_id: UUID, token: ClaimToken, message: str
) -> None:
    mark_job_failure_sync(
        settings, job_kind=JobKind.SOURCE_INDEX, job_id=job_id, token=token, message=message
    )
