from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    Job,
    JobKind,
    JobStatus,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeRevision,
    OutboxEvent,
    RetrievalIndexRun,
    Source,
    SourceStatus,
    SourceVersion,
)


@dataclass(frozen=True, slots=True)
class IndexRebuildOutcome:
    job: Job
    run: RetrievalIndexRun


class IndexingService:
    async def request_source_rebuild(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        space_id: UUID,
        source_version_id: UUID,
        idempotency_key: str,
    ) -> IndexRebuildOutcome:
        key = _validate_idempotency_key(idempotency_key)
        row = (
            await session.execute(
                select(SourceVersion, Source)
                .join(Source, Source.id == SourceVersion.source_id)
                .where(SourceVersion.id == source_version_id, Source.space_id == space_id)
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                "source_version_not_found", "Source version was not found.", status_code=404
            )
        version, _source = row
        if version.current_parse_artifact_id is None:
            raise AppError(
                "source_not_parsed",
                "Source version has no current parse artifact.",
                status_code=409,
            )
        input_hash = hashlib.sha256(
            f"{version.id}:{version.current_parse_artifact_id}:{version.content_sha256}".encode()
        ).hexdigest()
        existing = await session.scalar(
            select(RetrievalIndexRun)
            .join(Job, Job.id == RetrievalIndexRun.job_id)
            .where(
                Job.space_id == space_id,
                Job.idempotency_key == key,
                Job.kind == JobKind.SOURCE_INDEX.value,
            )
        )
        if existing is not None:
            if existing.target_kind != "source_version" or existing.target_id != source_version_id:
                raise AppError(
                    "idempotency_conflict",
                    "Idempotency-Key was already used for a different index target.",
                    status_code=409,
                )
            existing_job = await session.get(Job, existing.job_id)
            assert existing_job is not None
            return IndexRebuildOutcome(existing_job, existing)
        existing_target = await session.scalar(
            select(RetrievalIndexRun).where(
                RetrievalIndexRun.space_id == space_id,
                RetrievalIndexRun.target_kind == "source_version",
                RetrievalIndexRun.target_id == source_version_id,
                RetrievalIndexRun.input_hash == input_hash,
                RetrievalIndexRun.index_config_version == settings.index_version,
            )
        )
        if existing_target is not None:
            job = await session.get(Job, existing_target.job_id)
            assert job is not None
            return IndexRebuildOutcome(job, existing_target)
        root = await session.scalar(
            select(KnowledgeNode).where(
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.kind == KnowledgeNodeKind.ROOT.value,
                KnowledgeNode.deleted_at.is_(None),
            )
        )
        if root is None:
            raise AppError("space_not_found", "Knowledge space was not found.", status_code=404)
        source_node = await session.scalar(
            select(KnowledgeNode).where(
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.source_version_id == source_version_id,
                KnowledgeNode.kind == KnowledgeNodeKind.SOURCE.value,
                KnowledgeNode.deleted_at.is_(None),
            )
        )
        scope_node = source_node or root
        job = Job(
            id=uuid4(),
            space_id=space_id,
            source_version_id=source_version_id,
            kind=JobKind.SOURCE_INDEX.value,
            status=JobStatus.QUEUED.value,
            progress=0,
            idempotency_key=key,
        )
        run = RetrievalIndexRun(
            id=uuid4(),
            job_id=job.id,
            space_id=space_id,
            target_kind="source_version",
            target_id=source_version_id,
            scope_node_id=scope_node.id,
            input_hash=input_hash,
            source_version_id=source_version_id,
            parse_artifact_id=version.current_parse_artifact_id,
            knowledge_revision_id=None,
            status="queued",
            index_config_version=settings.index_version,
            chunker_version=settings.chunker_version,
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
            embedding_config={"dimensions": settings.embedding_dimensions},
            embedding_dimensions=settings.embedding_dimensions,
        )
        session.add_all([job, run])
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type="job.source_index.requested",
                deduplication_key=f"source-index:{run.id}",
                payload={"job_id": str(job.id), "space_id": str(space_id)},
            )
        )
        await session.flush()
        return IndexRebuildOutcome(job, run)

    async def request_knowledge_rebuild(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        space_id: UUID,
        revision_id: UUID,
        idempotency_key: str,
    ) -> IndexRebuildOutcome:
        key = _validate_idempotency_key(idempotency_key)
        row = (
            await session.execute(
                select(KnowledgeRevision, KnowledgeNode)
                .join(KnowledgeNode, KnowledgeNode.id == KnowledgeRevision.node_id)
                .where(
                    KnowledgeRevision.id == revision_id,
                    KnowledgeNode.space_id == space_id,
                    KnowledgeNode.current_revision_id == revision_id,
                    KnowledgeNode.deleted_at.is_(None),
                )
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                "knowledge_revision_not_current",
                "Current knowledge revision was not found.",
                status_code=404,
            )
        revision, node = row
        input_hash = revision.content_hash
        existing = await session.scalar(
            select(RetrievalIndexRun)
            .join(Job)
            .where(
                Job.space_id == space_id,
                Job.idempotency_key == key,
                Job.kind == JobKind.SOURCE_INDEX.value,
            )
        )
        if existing is not None:
            if existing.target_kind != "knowledge_revision" or existing.target_id != revision_id:
                raise AppError(
                    "idempotency_conflict",
                    "Idempotency-Key was already used for a different index target.",
                    status_code=409,
                )
            existing_job = await session.get(Job, existing.job_id)
            assert existing_job is not None
            return IndexRebuildOutcome(existing_job, existing)
        existing_target = await session.scalar(
            select(RetrievalIndexRun).where(
                RetrievalIndexRun.space_id == space_id,
                RetrievalIndexRun.target_kind == "knowledge_revision",
                RetrievalIndexRun.target_id == revision_id,
                RetrievalIndexRun.input_hash == input_hash,
                RetrievalIndexRun.index_config_version == settings.index_version,
            )
        )
        if existing_target is not None:
            existing_job = await session.get(Job, existing_target.job_id)
            assert existing_job is not None
            return IndexRebuildOutcome(existing_job, existing_target)
        job = Job(
            id=uuid4(),
            space_id=space_id,
            source_version_id=None,
            kind=JobKind.SOURCE_INDEX.value,
            status=JobStatus.QUEUED.value,
            progress=0,
            idempotency_key=key,
        )
        run = RetrievalIndexRun(
            id=uuid4(),
            job_id=job.id,
            space_id=space_id,
            target_kind="knowledge_revision",
            target_id=revision_id,
            scope_node_id=node.id,
            input_hash=input_hash,
            source_version_id=None,
            parse_artifact_id=None,
            knowledge_revision_id=revision_id,
            status="queued",
            index_config_version=settings.index_version,
            chunker_version=settings.chunker_version,
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
            embedding_config={"dimensions": settings.embedding_dimensions},
            embedding_dimensions=settings.embedding_dimensions,
        )
        session.add_all([job, run])
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type="job.source_index.requested",
                deduplication_key=f"knowledge-index:{run.id}",
                payload={"job_id": str(job.id), "space_id": str(space_id)},
            )
        )
        await session.flush()
        return IndexRebuildOutcome(job, run)

    async def request_space_rebuild(
        self, session: AsyncSession, *, settings: Settings, space_id: UUID, idempotency_key: str
    ) -> list[IndexRebuildOutcome]:
        root = await session.scalar(
            select(KnowledgeNode).where(
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.kind == KnowledgeNodeKind.ROOT.value,
                KnowledgeNode.deleted_at.is_(None),
            )
        )
        if root is None:
            raise AppError("space_not_found", "Knowledge space was not found.", status_code=404)
        latest_version = aliased(SourceVersion)
        newer_ready_version = aliased(SourceVersion)
        version_ids = list(
            await session.scalars(
                select(latest_version.id)
                .join(Source, Source.id == latest_version.source_id)
                .where(
                    Source.space_id == space_id,
                    Source.status == SourceStatus.ACTIVE.value,
                    Source.deleted_at.is_(None),
                    latest_version.current_parse_artifact_id.is_not(None),
                    ~select(newer_ready_version.id)
                    .where(
                        newer_ready_version.source_id == latest_version.source_id,
                        newer_ready_version.version_number > latest_version.version_number,
                        newer_ready_version.current_parse_artifact_id.is_not(None),
                    )
                    .exists(),
                )
            )
        )
        outcomes = []
        for version_id in version_ids:
            suffix = hashlib.sha256(str(version_id).encode()).hexdigest()[:12]
            outcomes.append(
                await self.request_source_rebuild(
                    session,
                    settings=settings,
                    space_id=space_id,
                    source_version_id=version_id,
                    idempotency_key=f"{idempotency_key}:source:{suffix}",
                )
            )
        revision_ids = list(
            await session.scalars(
                select(KnowledgeRevision.id)
                .join(KnowledgeNode)
                .where(
                    KnowledgeNode.space_id == space_id,
                    KnowledgeNode.current_revision_id == KnowledgeRevision.id,
                    KnowledgeNode.deleted_at.is_(None),
                )
            )
        )
        for revision_id in revision_ids:
            suffix = hashlib.sha256(str(revision_id).encode()).hexdigest()[:12]
            outcomes.append(
                await self.request_knowledge_rebuild(
                    session,
                    settings=settings,
                    space_id=space_id,
                    revision_id=revision_id,
                    idempotency_key=f"{idempotency_key}:knowledge:{suffix}",
                )
            )
        return outcomes


def index_config_snapshot(settings: Settings) -> dict[str, object]:
    return json.loads(
        json.dumps(
            {
                "index_config_version": settings.index_version,
                "chunker_version": settings.chunker_version,
                "chunk_target_characters": settings.chunk_target_characters,
                "chunk_overlap_characters": settings.chunk_overlap_characters,
                "embedding_provider": settings.embedding_provider,
                "embedding_model": settings.embedding_model,
                "embedding_dimensions": settings.embedding_dimensions,
            }
        )
    )
