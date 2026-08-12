from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeRevision,
    RetrievalChunk,
    RetrievalIndexRun,
    Source,
    SourceStatus,
    SourceVersion,
)


@dataclass(frozen=True, slots=True)
class ScopeRequest:
    scope_node_id: UUID | None
    include_descendants: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    space_id: UUID
    scope_node_id: UUID
    scope_ltree: str
    scope_path: str
    node_kind: str
    node_title: str
    include_descendants: bool
    knowledge_count: int
    source_count: int
    source_version_count: int
    chunk_count: int
    index_status: str
    index_config_version: str | None
    scope_snapshot_hash: str

    def snapshot(self) -> dict[str, object]:
        return {
            "scope_node_id": str(self.scope_node_id),
            "include_descendants": self.include_descendants,
            "scope_snapshot_hash": self.scope_snapshot_hash,
            "scope_path": self.scope_path,
            "node_kind": self.node_kind,
            "node_title": self.node_title,
            "knowledge_count": self.knowledge_count,
            "source_count": self.source_count,
            "source_version_count": self.source_version_count,
            "chunk_count": self.chunk_count,
            "index_status": self.index_status,
            "index_config_version": self.index_config_version,
        }


class ScopeResolver:
    async def resolve(
        self, session: AsyncSession, *, settings: Settings, space_id: UUID, request: ScopeRequest
    ) -> ResolvedScope:
        row = (
            await session.execute(
                select(KnowledgeNode, KnowledgeRevision.title, Source.title)
                .outerjoin(
                    KnowledgeRevision,
                    KnowledgeRevision.id == KnowledgeNode.current_revision_id,
                )
                .outerjoin(Source, Source.id == KnowledgeNode.source_id)
                .where(
                    KnowledgeNode.space_id == space_id,
                    KnowledgeNode.deleted_at.is_(None),
                    KnowledgeNode.kind == KnowledgeNodeKind.ROOT.value
                    if request.scope_node_id is None
                    else KnowledgeNode.id == request.scope_node_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise AppError("scope_not_found", "Retrieval scope was not found.", status_code=404)
        node, revision_title, source_title = row
        node_title = (
            "整个知识库"
            if node.kind == KnowledgeNodeKind.ROOT.value
            else revision_title or source_title or node.kind
        )
        scope_path = await self._breadcrumb(
            session,
            space_id=space_id,
            node_path=node.path,
            fallback=node_title,
        )
        predicates = self.scope_predicates(
            settings=settings,
            space_id=space_id,
            scope_path=node.path,
            include_descendants=request.include_descendants,
        )
        counts = (
            await session.execute(
                select(
                    func.count(func.distinct(RetrievalChunk.knowledge_node_id)),
                    func.count(func.distinct(RetrievalChunk.source_id)),
                    func.count(func.distinct(RetrievalChunk.source_version_id)),
                    func.count(RetrievalChunk.id),
                ).where(*predicates)
            )
        ).one()
        knowledge_count, source_count, source_version_count, chunk_count = map(int, counts)
        run_rows = (
            await session.execute(
                select(RetrievalIndexRun.status, func.count())
                .where(
                    RetrievalIndexRun.space_id == space_id,
                    RetrievalIndexRun.index_config_version == settings.index_version,
                )
                .group_by(RetrievalIndexRun.status)
            )
        ).all()
        run_counts: dict[str, int] = {status: int(count) for status, count in run_rows}
        index_status = self.index_status(chunk_count=chunk_count, run_counts=run_counts)
        index_config_version = settings.index_version if chunk_count else None
        snapshot_payload = {
            "space_id": str(space_id),
            "scope_node_id": str(node.id),
            "scope_ltree": str(node.path),
            "include_descendants": request.include_descendants,
            "knowledge_count": knowledge_count,
            "source_count": source_count,
            "source_version_count": source_version_count,
            "chunk_count": chunk_count,
            "index_status": index_status,
            "index_config_version": index_config_version,
        }
        scope_snapshot_hash = hashlib.sha256(
            json.dumps(snapshot_payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return ResolvedScope(
            space_id=space_id,
            scope_node_id=node.id,
            scope_ltree=str(node.path),
            scope_path=scope_path,
            node_kind=node.kind,
            node_title=node_title,
            include_descendants=request.include_descendants,
            knowledge_count=knowledge_count,
            source_count=source_count,
            source_version_count=source_version_count,
            chunk_count=chunk_count,
            index_status=index_status,
            index_config_version=index_config_version,
            scope_snapshot_hash=scope_snapshot_hash,
        )

    @staticmethod
    async def _breadcrumb(
        session: AsyncSession, *, space_id: UUID, node_path: str, fallback: str
    ) -> str:
        ancestor = aliased(KnowledgeNode)
        revision = aliased(KnowledgeRevision)
        source = aliased(Source)
        labels = list(
            await session.scalars(
                select(func.coalesce(revision.title, source.title))
                .select_from(ancestor)
                .outerjoin(revision, revision.id == ancestor.current_revision_id)
                .outerjoin(source, source.id == ancestor.source_id)
                .where(
                    ancestor.space_id == space_id,
                    ancestor.deleted_at.is_(None),
                    ancestor.path.op("@>")(node_path),
                )
                .order_by(ancestor.path)
            )
        )
        normalized = [label for label in labels if label]
        return " / ".join(normalized) or fallback

    @staticmethod
    def index_status(*, chunk_count: int, run_counts: dict[str, int]) -> str:
        if run_counts.get("queued", 0) or run_counts.get("running", 0):
            return "building"
        if run_counts.get("failed", 0):
            return "degraded" if chunk_count else "failed"
        return "ready" if chunk_count else "empty"

    @staticmethod
    def scope_predicates(
        *, settings: Settings, space_id: UUID, scope_path: str, include_descendants: bool
    ) -> tuple[ColumnElement[bool], ...]:
        path_predicate = (
            RetrievalChunk.path.op("<@")(scope_path)
            if include_descendants
            else RetrievalChunk.path == scope_path
        )
        current_knowledge = exists(
            select(KnowledgeNode.id).where(
                KnowledgeNode.id == RetrievalChunk.knowledge_node_id,
                KnowledgeNode.space_id == RetrievalChunk.space_id,
                KnowledgeNode.current_revision_id == RetrievalChunk.knowledge_revision_id,
                KnowledgeNode.deleted_at.is_(None),
            )
        )
        latest_version = aliased(SourceVersion)
        newer_ready_version = aliased(SourceVersion)
        current_source = and_(
            exists(
                select(latest_version.id)
                .join(Source, Source.id == latest_version.source_id)
                .where(
                    latest_version.id == RetrievalChunk.source_version_id,
                    latest_version.current_parse_artifact_id
                    == RetrievalChunk.parse_artifact_id,
                    Source.space_id == RetrievalChunk.space_id,
                    Source.deleted_at.is_(None),
                    Source.status == SourceStatus.ACTIVE.value,
                    ~exists(
                        select(newer_ready_version.id).where(
                            newer_ready_version.source_id == latest_version.source_id,
                            newer_ready_version.version_number
                            > latest_version.version_number,
                            newer_ready_version.current_parse_artifact_id.is_not(None),
                        )
                    ),
                )
            )
        )
        current_identity = or_(
            and_(RetrievalChunk.corpus_kind == "confirmed_knowledge", current_knowledge),
            and_(RetrievalChunk.corpus_kind == "source_evidence", current_source),
        )
        return (
            RetrievalChunk.space_id == space_id,
            RetrievalChunk.active.is_(True),
            RetrievalChunk.index_config_version == settings.index_version,
            RetrievalChunk.embedding_model == settings.embedding_model,
            path_predicate,
            current_identity,
        )
