from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    ActivityEvent,
    ActorType,
    KnowledgeEvidence,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeRevision,
    KnowledgeSpace,
    KnowledgeWriteRequest,
    KnowledgeWriteResult,
    OutboxEvent,
    ReviewCard,
    Source,
    SourceParseArtifact,
    SourceSection,
    SourceVersion,
)

LOCAL_ACTOR = "local"
CREATABLE_KINDS = {
    KnowledgeNodeKind.FOLDER.value,
    KnowledgeNodeKind.DOCUMENT.value,
}
EDITABLE_KINDS = {
    KnowledgeNodeKind.DOCUMENT.value,
    KnowledgeNodeKind.POINT.value,
}
MUTABLE_KINDS = {
    KnowledgeNodeKind.FOLDER.value,
    KnowledgeNodeKind.DOCUMENT.value,
    KnowledgeNodeKind.POINT.value,
}
LEGAL_PARENT_KINDS = {
    KnowledgeNodeKind.FOLDER.value: {
        KnowledgeNodeKind.ROOT.value,
        KnowledgeNodeKind.FOLDER.value,
    },
    KnowledgeNodeKind.DOCUMENT.value: {
        KnowledgeNodeKind.ROOT.value,
        KnowledgeNodeKind.FOLDER.value,
    },
    KnowledgeNodeKind.POINT.value: {KnowledgeNodeKind.DOCUMENT.value},
}


class KnowledgeWriteOperation(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    MOVE = "move"
    DELETE = "delete"


class KnowledgeNodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: UUID | None = None
    kind: KnowledgeNodeKind
    expected_version: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(default="", max_length=20_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    conditions: list[str] = Field(default_factory=list, max_length=12)
    exceptions: list[str] = Field(default_factory=list, max_length=12)


class KnowledgeNodeEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)
    expected_revision_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, max_length=20_000)
    tags: list[str] | None = Field(default=None, max_length=20)
    conditions: list[str] | None = Field(default=None, max_length=12)
    exceptions: list[str] | None = Field(default=None, max_length=12)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_change(self) -> KnowledgeNodeEdit:
        mutable_fields = self.model_fields_set - {
            "expected_version",
            "expected_revision_id",
            "reason",
        }
        if not any(getattr(self, field) is not None for field in mutable_fields):
            raise ValueError("At least one revision field must be supplied")
        return self


class KnowledgeNodeMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)
    parent_id: UUID | None = None


class KnowledgeNodeDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class KnowledgeNodeRecord:
    node: KnowledgeNode
    revision: KnowledgeRevision | None
    source_title: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeNodeDetail:
    node: KnowledgeNode
    revision: KnowledgeRevision | None
    evidence: list[KnowledgeEvidenceRecord]
    source_title: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeSearchRecord:
    record: KnowledgeNodeRecord
    breadcrumb: str
    ancestor_ids: list[UUID]
    match_fields: list[str]


@dataclass(frozen=True, slots=True)
class KnowledgeWriteOutcome:
    result_id: UUID
    request_id: UUID
    node_id: UUID
    node_version: int
    snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceRecord:
    evidence: KnowledgeEvidence
    node_id: UUID
    revision_number: int
    source_id: UUID
    source_title: str
    source_version_number: int
    source_content_hash: str | None
    artifact_revision: int
    section_ordinal: int


def knowledge_request_hash(
    operation: KnowledgeWriteOperation,
    request: BaseModel,
    *,
    node_id: UUID | None = None,
) -> str:
    payload: dict[str, Any] = {
        "operation": operation.value,
        "request": request.model_dump(mode="json", exclude_unset=True),
    }
    if node_id is not None:
        payload["node_id"] = str(node_id)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def revision_content_hash(
    *,
    title: str,
    body: str,
    tags: list[str],
    conditions: list[str],
    exceptions: list[str],
) -> str:
    canonical = json.dumps(
        {
            "body": body,
            "conditions": conditions,
            "exceptions": exceptions,
            "tags": tags,
            "title": title,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class KnowledgeTreeService:
    async def list_tree(
        self, session: AsyncSession, *, space_id: UUID
    ) -> list[KnowledgeNodeRecord]:
        rows = list(
            (
                await session.execute(
                    select(KnowledgeNode, KnowledgeRevision, Source.title)
                    .outerjoin(
                        KnowledgeRevision,
                        KnowledgeRevision.id == KnowledgeNode.current_revision_id,
                    )
                    .outerjoin(Source, Source.id == KnowledgeNode.source_id)
                    .where(
                        KnowledgeNode.space_id == space_id,
                        KnowledgeNode.deleted_at.is_(None),
                    )
                    .order_by(KnowledgeNode.path, KnowledgeNode.sort_order, KnowledgeNode.id)
                )
            ).all()
        )
        return [
            KnowledgeNodeRecord(node, revision, source_title)
            for node, revision, source_title in rows
        ]

    async def get_node(
        self, session: AsyncSession, *, space_id: UUID, node_id: UUID
    ) -> KnowledgeNodeDetail:
        row = (
            await session.execute(
                select(KnowledgeNode, KnowledgeRevision, Source.title)
                .outerjoin(
                    KnowledgeRevision,
                    KnowledgeRevision.id == KnowledgeNode.current_revision_id,
                )
                .outerjoin(Source, Source.id == KnowledgeNode.source_id)
                .where(
                    KnowledgeNode.id == node_id,
                    KnowledgeNode.space_id == space_id,
                    KnowledgeNode.deleted_at.is_(None),
                )
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                "knowledge_node_not_found",
                "Knowledge node was not found.",
                status_code=404,
            )
        node, revision, source_title = row
        evidence: list[KnowledgeEvidenceRecord] = []
        if revision is not None:
            evidence_rows = (
                await session.execute(
                    self._evidence_statement(space_id=space_id).where(
                        KnowledgeEvidence.revision_id == revision.id
                    )
                )
            ).all()
            evidence = [self._evidence_record(item) for item in evidence_rows]
        return KnowledgeNodeDetail(node, revision, evidence, source_title)

    async def search(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        query: str,
        limit: int = 50,
    ) -> list[KnowledgeSearchRecord]:
        cleaned = query.strip()
        if not cleaned:
            raise AppError(
                "invalid_knowledge_search",
                "Search query must not be blank.",
                status_code=422,
            )
        pattern = f"%{cleaned}%"
        # Correlate ancestors through ltree containment while preserving one row per target.
        ancestor = KnowledgeNode.__table__.alias("knowledge_ancestor")
        ancestor_revision = KnowledgeRevision.__table__.alias("knowledge_ancestor_revision")
        ancestor_source = Source.__table__.alias("knowledge_ancestor_source")
        path_titles = (
            select(
                KnowledgeNode.id.label("target_id"),
                func.string_agg(
                    func.coalesce(
                        ancestor_revision.c.title,
                        ancestor_source.c.title,
                        "",
                    ),
                    aggregate_order_by(literal(" / "), ancestor.c.path),
                ).label("breadcrumb"),
                func.array_agg(
                    aggregate_order_by(ancestor.c.id, ancestor.c.path)
                ).label("ancestor_ids"),
            )
            .select_from(KnowledgeNode)
            .join(
                ancestor,
                ancestor.c.path.op("@>")(KnowledgeNode.path),
            )
            .outerjoin(
                ancestor_revision,
                ancestor_revision.c.id == ancestor.c.current_revision_id,
            )
            .outerjoin(ancestor_source, ancestor_source.c.id == ancestor.c.source_id)
            .where(
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.deleted_at.is_(None),
                ancestor.c.space_id == space_id,
                ancestor.c.deleted_at.is_(None),
            )
            .group_by(KnowledgeNode.id)
            .cte("knowledge_path_titles")
        )
        evidence_source_titles = (
            select(
                KnowledgeNode.id.label("node_id"),
                func.string_agg(
                    func.distinct(Source.title),
                    literal(" / "),
                ).label("source_titles"),
            )
            .select_from(KnowledgeNode)
            .join(
                KnowledgeRevision,
                KnowledgeRevision.id == KnowledgeNode.current_revision_id,
            )
            .join(
                KnowledgeEvidence,
                and_(
                    KnowledgeEvidence.revision_id == KnowledgeRevision.id,
                    KnowledgeEvidence.space_id == KnowledgeNode.space_id,
                ),
            )
            .join(
                SourceVersion,
                SourceVersion.id == KnowledgeEvidence.source_version_id,
            )
            .join(Source, Source.id == SourceVersion.source_id)
            .where(
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.deleted_at.is_(None),
                KnowledgeRevision.space_id == space_id,
                KnowledgeEvidence.space_id == space_id,
                Source.space_id == space_id,
            )
            .group_by(KnowledgeNode.id)
            .cte("knowledge_evidence_source_titles")
        )
        rows = list(
            (
                await session.execute(
                    select(
                        KnowledgeNode,
                        KnowledgeRevision,
                        Source.title,
                        evidence_source_titles.c.source_titles,
                        path_titles.c.breadcrumb,
                        path_titles.c.ancestor_ids,
                    )
                    .join(path_titles, path_titles.c.target_id == KnowledgeNode.id)
                    .outerjoin(
                        KnowledgeRevision,
                        KnowledgeRevision.id == KnowledgeNode.current_revision_id,
                    )
                    .outerjoin(Source, Source.id == KnowledgeNode.source_id)
                    .outerjoin(
                        evidence_source_titles,
                        evidence_source_titles.c.node_id == KnowledgeNode.id,
                    )
                    .where(
                        KnowledgeNode.space_id == space_id,
                        KnowledgeNode.deleted_at.is_(None),
                        or_(
                            KnowledgeRevision.title.ilike(pattern),
                            KnowledgeRevision.body.ilike(pattern),
                            path_titles.c.breadcrumb.ilike(pattern),
                            Source.title.ilike(pattern),
                            evidence_source_titles.c.source_titles.ilike(pattern),
                        ),
                    )
                    .order_by(KnowledgeNode.path, KnowledgeNode.id)
                    .limit(limit)
                )
            ).all()
        )
        records: list[KnowledgeSearchRecord] = []
        normalized = cleaned.casefold()
        for (
            node,
            revision,
            source_title,
            evidence_source_title,
            breadcrumb,
            ancestor_ids,
        ) in rows:
            match_fields: list[str] = []
            if revision is not None and normalized in revision.title.casefold():
                match_fields.append("title")
            if revision is not None and normalized in revision.body.casefold():
                match_fields.append("body")
            if breadcrumb and normalized in breadcrumb.casefold():
                match_fields.append("path")
            if (
                (source_title and normalized in source_title.casefold())
                or (
                    evidence_source_title
                    and normalized in evidence_source_title.casefold()
                )
            ):
                match_fields.append("source_title")
            records.append(
                KnowledgeSearchRecord(
                    record=KnowledgeNodeRecord(node, revision, source_title),
                    breadcrumb=breadcrumb or "",
                    ancestor_ids=list(ancestor_ids or [])[:-1],
                    match_fields=match_fields,
                )
            )
        return records

    async def get_write_request(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        idempotency_key: str,
    ) -> tuple[KnowledgeWriteRequest, KnowledgeWriteOutcome]:
        key = _validate_idempotency_key(idempotency_key)
        row = (
            await session.execute(
                select(KnowledgeWriteRequest, KnowledgeWriteResult)
                .join(
                    KnowledgeWriteResult,
                    KnowledgeWriteResult.request_id == KnowledgeWriteRequest.id,
                )
                .where(
                    KnowledgeWriteRequest.space_id == space_id,
                    KnowledgeWriteRequest.idempotency_key == key,
                )
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                "knowledge_write_request_not_found",
                "Knowledge write request was not found.",
                status_code=404,
            )
        request_row, result = row
        return request_row, self._outcome(result)

    async def get_evidence(
        self, session: AsyncSession, *, space_id: UUID, evidence_id: UUID
    ) -> KnowledgeEvidenceRecord:
        row = (
            await session.execute(
                self._evidence_statement(space_id=space_id).where(
                    KnowledgeEvidence.id == evidence_id
                )
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                "knowledge_evidence_not_found",
                "Knowledge evidence was not found.",
                status_code=404,
            )
        return self._evidence_record(row)

    @staticmethod
    def _evidence_statement(*, space_id: UUID) -> Any:
        return (
            select(
                KnowledgeEvidence,
                KnowledgeRevision.node_id,
                KnowledgeRevision.revision_number,
                Source.id,
                Source.title,
                SourceVersion.version_number,
                SourceVersion.content_sha256,
                SourceParseArtifact.revision,
                SourceSection.ordinal,
            )
            .join(
                KnowledgeRevision,
                and_(
                    KnowledgeRevision.id == KnowledgeEvidence.revision_id,
                    KnowledgeRevision.space_id == KnowledgeEvidence.space_id,
                ),
            )
            .join(
                KnowledgeNode,
                and_(
                    KnowledgeNode.id == KnowledgeRevision.node_id,
                    KnowledgeNode.space_id == KnowledgeRevision.space_id,
                ),
            )
            .join(
                SourceVersion,
                SourceVersion.id == KnowledgeEvidence.source_version_id,
            )
            .join(Source, Source.id == SourceVersion.source_id)
            .join(
                SourceParseArtifact,
                and_(
                    SourceParseArtifact.id == KnowledgeEvidence.parse_artifact_id,
                    SourceParseArtifact.source_version_id
                    == KnowledgeEvidence.source_version_id,
                    SourceParseArtifact.status == "ready",
                ),
            )
            .join(
                SourceSection,
                and_(
                    SourceSection.id == KnowledgeEvidence.section_id,
                    SourceSection.source_version_id
                    == KnowledgeEvidence.source_version_id,
                    SourceSection.parse_artifact_id
                    == KnowledgeEvidence.parse_artifact_id,
                    SourceSection.quote_hash == KnowledgeEvidence.quote_hash,
                    SourceSection.content_hash == KnowledgeEvidence.content_hash,
                ),
            )
            .where(
                KnowledgeEvidence.space_id == space_id,
                KnowledgeRevision.space_id == space_id,
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.deleted_at.is_(None),
                Source.space_id == space_id,
            )
            .order_by(KnowledgeEvidence.created_at, KnowledgeEvidence.id)
        )

    @staticmethod
    def _evidence_record(row: Any) -> KnowledgeEvidenceRecord:
        (
            evidence,
            node_id,
            revision_number,
            source_id,
            source_title,
            source_version_number,
            source_content_hash,
            artifact_revision,
            section_ordinal,
        ) = row
        return KnowledgeEvidenceRecord(
            evidence=evidence,
            node_id=node_id,
            revision_number=revision_number,
            source_id=source_id,
            source_title=source_title,
            source_version_number=source_version_number,
            source_content_hash=source_content_hash,
            artifact_revision=artifact_revision,
            section_ordinal=section_ordinal,
        )

    async def create(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        idempotency_key: str,
        request: KnowledgeNodeCreate,
    ) -> KnowledgeWriteOutcome:
        operation = KnowledgeWriteOperation.CREATE
        key = _validate_idempotency_key(idempotency_key)
        request_hash = knowledge_request_hash(operation, request)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        await self._lock_space(session, space_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        if request.kind.value not in CREATABLE_KINDS:
            raise AppError(
                "immutable_knowledge_node_kind",
                "Only folder and document nodes can be created through this endpoint.",
                status_code=409,
            )
        parent = await self._lock_parent(session, space_id=space_id, parent_id=request.parent_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        self._validate_version(parent, request.expected_version)
        self._validate_parent(request.kind.value, parent)

        node = KnowledgeNode(
            id=uuid4(),
            space_id=space_id,
            parent_id=parent.id,
            kind=request.kind.value,
            path="pending",
            version=1,
            sort_order=await self._next_sort_order(session, space_id, parent.id),
        )
        # The path label must be derived from the durable node identity.
        node.path = f"{parent.path}.{_path_label(node.id)}"
        session.add(node)
        await session.flush()
        revision = self._new_revision(
            node=node,
            revision_number=1,
            title=request.title,
            body=request.body,
            tags=request.tags,
            conditions=request.conditions,
            exceptions=request.exceptions,
            edit_reason=None,
        )
        session.add(revision)
        await session.flush()
        node.current_revision_id = revision.id
        await session.flush()
        return await self._record_write(
            session,
            operation=operation,
            key=key,
            request_hash=request_hash,
            node=node,
            revision=revision,
            evidence_ids=[],
        )

    async def edit(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        node_id: UUID,
        idempotency_key: str,
        request: KnowledgeNodeEdit,
    ) -> KnowledgeWriteOutcome:
        operation = KnowledgeWriteOperation.EDIT
        key = _validate_idempotency_key(idempotency_key)
        request_hash = knowledge_request_hash(operation, request, node_id=node_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        await self._lock_space(session, space_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        node = await self._lock_node(session, space_id=space_id, node_id=node_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        self._validate_mutable(node)
        if node.kind not in EDITABLE_KINDS:
            raise AppError(
                "knowledge_node_has_no_body",
                "Only document and point nodes can append revisions.",
                status_code=409,
            )
        self._validate_version(node, request.expected_version)
        current = await self._current_revision(session, node)
        if current is None:
            raise AppError(
                "knowledge_revision_missing",
                "The current knowledge revision is unavailable.",
                status_code=409,
            )
        if current.id != request.expected_revision_id:
            raise AppError(
                "knowledge_revision_conflict",
                "Current revision does not match expected_revision_id.",
                status_code=409,
                details=[
                    {
                        "expected_revision_id": str(request.expected_revision_id),
                        "actual_revision_id": str(current.id),
                    }
                ],
            )

        revision = self._new_revision(
            node=node,
            revision_number=current.revision_number + 1,
            title=(
                _required_text(request.title, "title")
                if request.title is not None
                else current.title
            ),
            body=request.body if request.body is not None else current.body,
            tags=_clean_list(request.tags) if request.tags is not None else list(current.tags),
            conditions=(
                _clean_list(request.conditions)
                if request.conditions is not None
                else list(current.conditions)
            ),
            exceptions=(
                _clean_list(request.exceptions)
                if request.exceptions is not None
                else list(current.exceptions)
            ),
            edit_reason=(
                request.reason.strip()
                if request.reason and request.reason.strip()
                else None
            ),
        )
        session.add(revision)
        await session.flush()
        old_evidence = list(
            await session.scalars(
                select(KnowledgeEvidence)
                .where(
                    KnowledgeEvidence.revision_id == current.id,
                    KnowledgeEvidence.space_id == space_id,
                )
                .order_by(KnowledgeEvidence.created_at, KnowledgeEvidence.id)
            )
        )
        copied_evidence = [
            KnowledgeEvidence(
                id=uuid4(),
                revision_id=revision.id,
                space_id=space_id,
                source_version_id=item.source_version_id,
                parse_artifact_id=item.parse_artifact_id,
                section_id=item.section_id,
                quote_hash=item.quote_hash,
                content_hash=item.content_hash,
                locator=dict(item.locator),
                frozen_quote=item.frozen_quote,
            )
            for item in old_evidence
        ]
        session.add_all(copied_evidence)
        node.current_revision_id = revision.id
        node.version += 1
        card = await session.scalar(
            select(ReviewCard).where(
                ReviewCard.knowledge_node_id == node.id,
                ReviewCard.space_id == space_id,
            )
        )
        if card is not None:
            card.knowledge_revision_id = revision.id
        await session.flush()
        return await self._record_write(
            session,
            operation=operation,
            key=key,
            request_hash=request_hash,
            node=node,
            revision=revision,
            evidence_ids=[item.id for item in copied_evidence],
        )

    async def move(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        node_id: UUID,
        idempotency_key: str,
        request: KnowledgeNodeMove,
    ) -> KnowledgeWriteOutcome:
        operation = KnowledgeWriteOperation.MOVE
        key = _validate_idempotency_key(idempotency_key)
        request_hash = knowledge_request_hash(operation, request, node_id=node_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        await self._lock_space(session, space_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        node = await self._lock_node(session, space_id=space_id, node_id=node_id)
        parent = await self._lock_parent(session, space_id=space_id, parent_id=request.parent_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        self._validate_mutable(node)
        self._validate_version(node, request.expected_version)
        self._validate_parent(node.kind, parent)
        if _is_same_or_descendant(parent.path, node.path):
            raise AppError(
                "knowledge_tree_cycle",
                "A node cannot be moved under itself or its descendant.",
                status_code=409,
            )

        descendants = list(
            await session.scalars(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.space_id == space_id,
                    KnowledgeNode.deleted_at.is_(None),
                    KnowledgeNode.path.op("<@")(node.path),
                )
                .order_by(KnowledgeNode.path)
                .with_for_update()
            )
        )
        old_path = node.path
        new_path = f"{parent.path}.{_path_label(node.id)}"
        for descendant in descendants:
            descendant.path = _replace_path_prefix(descendant.path, old_path, new_path)
        node.parent_id = parent.id
        node.sort_order = await self._next_sort_order(session, space_id, parent.id)
        node.version += 1
        await session.flush()
        revision = await self._current_revision(session, node, required=False)
        return await self._record_write(
            session,
            operation=operation,
            key=key,
            request_hash=request_hash,
            node=node,
            revision=revision,
            evidence_ids=[],
        )

    async def delete(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        node_id: UUID,
        idempotency_key: str,
        request: KnowledgeNodeDelete,
    ) -> KnowledgeWriteOutcome:
        operation = KnowledgeWriteOperation.DELETE
        key = _validate_idempotency_key(idempotency_key)
        request_hash = knowledge_request_hash(operation, request, node_id=node_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        await self._lock_space(session, space_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        node = await self._lock_node(session, space_id=space_id, node_id=node_id)
        replay = await self._replay(
            session, space_id=space_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        self._validate_mutable(node)
        self._validate_version(node, request.expected_version)
        subtree = list(
            await session.scalars(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.space_id == space_id,
                    KnowledgeNode.deleted_at.is_(None),
                    KnowledgeNode.path.op("<@")(node.path),
                )
                .order_by(KnowledgeNode.path)
                .with_for_update()
            )
        )
        deleted_at = datetime.now(UTC)
        for item in subtree:
            item.deleted_at = deleted_at
            item.version += 1
        subtree_ids = [item.id for item in subtree]
        cards = list(
            await session.scalars(
                select(ReviewCard).where(
                    ReviewCard.space_id == space_id,
                    ReviewCard.knowledge_node_id.in_(subtree_ids),
                )
            )
        )
        for card in cards:
            card.status = "retired"
        await session.flush()
        revision = await self._current_revision(session, node, required=False)
        return await self._record_write(
            session,
            operation=operation,
            key=key,
            request_hash=request_hash,
            node=node,
            revision=revision,
            evidence_ids=[],
        )

    async def _replay(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        key: str,
        request_hash: str,
    ) -> KnowledgeWriteOutcome | None:
        row = (
            await session.execute(
                select(KnowledgeWriteRequest, KnowledgeWriteResult)
                .join(
                    KnowledgeWriteResult,
                    KnowledgeWriteResult.request_id == KnowledgeWriteRequest.id,
                )
                .where(
                    KnowledgeWriteRequest.space_id == space_id,
                    KnowledgeWriteRequest.idempotency_key == key,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        request_row, result = row
        if request_row.request_hash != request_hash:
            raise AppError(
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request.",
                status_code=409,
            )
        return self._outcome(result)

    async def _record_write(
        self,
        session: AsyncSession,
        *,
        operation: KnowledgeWriteOperation,
        key: str,
        request_hash: str,
        node: KnowledgeNode,
        revision: KnowledgeRevision | None,
        evidence_ids: list[UUID],
    ) -> KnowledgeWriteOutcome:
        request_row = KnowledgeWriteRequest(
            id=uuid4(),
            space_id=node.space_id,
            idempotency_key=key,
            operation=operation.value,
            request_hash=request_hash,
        )
        snapshot = _write_snapshot(node, revision, evidence_ids)
        result = KnowledgeWriteResult(
            id=uuid4(),
            request_id=request_row.id,
            node_id=node.id,
            node_version=node.version,
            snapshot=snapshot,
        )
        event_type = {
            KnowledgeWriteOperation.CREATE: "knowledge_node.created",
            KnowledgeWriteOperation.EDIT: "knowledge_node.edited",
            KnowledgeWriteOperation.MOVE: "knowledge_node.moved",
            KnowledgeWriteOperation.DELETE: "knowledge_node.deleted",
        }[operation]
        payload = {
            "knowledge_node_id": str(node.id),
            "node_version": node.version,
            "operation": operation.value,
            "parent_id": str(node.parent_id) if node.parent_id else None,
            "revision_id": str(revision.id) if revision else None,
        }
        session.add_all(
            [
                request_row,
                result,
                ActivityEvent(
                    id=uuid4(),
                    space_id=node.space_id,
                    event_type=event_type,
                    entity_type="knowledge_node",
                    entity_id=node.id,
                    actor_type=ActorType.USER.value,
                    payload=payload,
                ),
                OutboxEvent(
                    id=uuid4(),
                    space_id=node.space_id,
                    aggregate_type="knowledge_node",
                    aggregate_id=node.id,
                    event_type=event_type,
                    deduplication_key=f"knowledge-write:{request_row.id}",
                    payload=payload,
                ),
            ]
        )
        await session.flush()
        return self._outcome(result)

    @staticmethod
    def _outcome(result: KnowledgeWriteResult) -> KnowledgeWriteOutcome:
        return KnowledgeWriteOutcome(
            result_id=result.id,
            request_id=result.request_id,
            node_id=result.node_id,
            node_version=result.node_version,
            snapshot=dict(result.snapshot),
        )

    @staticmethod
    async def _lock_space(session: AsyncSession, space_id: UUID) -> KnowledgeSpace:
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(str(space_id))))
        )
        space = await session.scalar(
            select(KnowledgeSpace).where(KnowledgeSpace.id == space_id).with_for_update()
        )
        if space is None:
            raise AppError(
                "knowledge_space_not_found",
                "Knowledge space was not found.",
                status_code=404,
            )
        return space

    @staticmethod
    async def _lock_node(
        session: AsyncSession, *, space_id: UUID, node_id: UUID
    ) -> KnowledgeNode:
        node = await session.scalar(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.id == node_id,
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if node is None:
            raise AppError(
                "knowledge_node_not_found",
                "Knowledge node was not found.",
                status_code=404,
            )
        return node

    @staticmethod
    async def _lock_parent(
        session: AsyncSession, *, space_id: UUID, parent_id: UUID | None
    ) -> KnowledgeNode:
        statement = select(KnowledgeNode).where(
            KnowledgeNode.space_id == space_id,
            KnowledgeNode.deleted_at.is_(None),
        )
        if parent_id is None:
            statement = statement.where(KnowledgeNode.kind == KnowledgeNodeKind.ROOT.value)
        else:
            statement = statement.where(KnowledgeNode.id == parent_id)
        parent = await session.scalar(statement.with_for_update())
        if parent is None:
            raise AppError(
                "knowledge_parent_not_found",
                "Parent knowledge node was not found in this space.",
                status_code=404,
            )
        return parent

    @staticmethod
    async def _current_revision(
        session: AsyncSession, node: KnowledgeNode, *, required: bool = True
    ) -> KnowledgeRevision | None:
        revision = (
            None
            if node.current_revision_id is None
            else await session.scalar(
                select(KnowledgeRevision).where(
                    KnowledgeRevision.id == node.current_revision_id,
                    KnowledgeRevision.node_id == node.id,
                    KnowledgeRevision.space_id == node.space_id,
                )
            )
        )
        if revision is None and required:
            raise AppError(
                "knowledge_revision_missing",
                "The current knowledge revision is unavailable.",
                status_code=409,
            )
        return revision

    @staticmethod
    async def _next_sort_order(
        session: AsyncSession, space_id: UUID, parent_id: UUID
    ) -> int:
        current = await session.scalar(
            select(func.max(KnowledgeNode.sort_order)).where(
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.parent_id == parent_id,
                KnowledgeNode.deleted_at.is_(None),
            )
        )
        return (current if current is not None else -1) + 1

    @staticmethod
    def _new_revision(
        *,
        node: KnowledgeNode,
        revision_number: int,
        title: str,
        body: str,
        tags: list[str],
        conditions: list[str],
        exceptions: list[str],
        edit_reason: str | None,
    ) -> KnowledgeRevision:
        cleaned_title = _required_text(title, "title")
        cleaned_tags = _clean_list(tags)
        cleaned_conditions = _clean_list(conditions)
        cleaned_exceptions = _clean_list(exceptions)
        return KnowledgeRevision(
            id=uuid4(),
            node_id=node.id,
            space_id=node.space_id,
            revision_number=revision_number,
            title=cleaned_title,
            body=body,
            tags=cleaned_tags,
            conditions=cleaned_conditions,
            exceptions=cleaned_exceptions,
            actor=LOCAL_ACTOR,
            content_hash=revision_content_hash(
                title=cleaned_title,
                body=body,
                tags=cleaned_tags,
                conditions=cleaned_conditions,
                exceptions=cleaned_exceptions,
            ),
            edit_reason=edit_reason,
        )

    @staticmethod
    def _validate_version(node: KnowledgeNode, expected_version: int) -> None:
        if node.version != expected_version:
            raise AppError(
                "knowledge_node_version_conflict",
                "Knowledge node version does not match expected_version.",
                status_code=409,
                details=[
                    {"expected_version": expected_version, "actual_version": node.version}
                ],
            )

    @staticmethod
    def _validate_mutable(node: KnowledgeNode) -> None:
        if node.kind not in MUTABLE_KINDS:
            raise AppError(
                "immutable_knowledge_node",
                "Root and source nodes cannot be changed.",
                status_code=409,
            )

    @staticmethod
    def _validate_parent(kind: str, parent: KnowledgeNode) -> None:
        if parent.kind not in LEGAL_PARENT_KINDS.get(kind, set()):
            raise AppError(
                "invalid_knowledge_parent",
                f"A {kind} node cannot be placed under a {parent.kind} node.",
                status_code=409,
            )


def _path_label(node_id: UUID) -> str:
    return f"n{node_id.hex}"


def _is_same_or_descendant(candidate_path: str, ancestor_path: str) -> bool:
    candidate = candidate_path.split(".")
    ancestor = ancestor_path.split(".")
    return candidate[: len(ancestor)] == ancestor


def _replace_path_prefix(path: str, old_prefix: str, new_prefix: str) -> str:
    if path == old_prefix:
        return new_prefix
    if not path.startswith(f"{old_prefix}."):
        raise ValueError("path is outside the moved subtree")
    return f"{new_prefix}{path[len(old_prefix):]}"


def _required_text(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise AppError(
            "invalid_knowledge_payload",
            f"{field} must not be blank.",
            status_code=422,
        )
    return cleaned


def _clean_list(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def _write_snapshot(
    node: KnowledgeNode,
    revision: KnowledgeRevision | None,
    evidence_ids: list[UUID],
) -> dict[str, Any]:
    return {
        "node": {
            "id": str(node.id),
            "space_id": str(node.space_id),
            "parent_id": str(node.parent_id) if node.parent_id else None,
            "kind": node.kind,
            "path": node.path,
            "version": node.version,
            "sort_order": node.sort_order,
            "current_revision_id": (
                str(node.current_revision_id) if node.current_revision_id else None
            ),
            "source_id": str(node.source_id) if node.source_id else None,
            "source_version_id": (
                str(node.source_version_id) if node.source_version_id else None
            ),
            "deleted_at": node.deleted_at.isoformat() if node.deleted_at else None,
        },
        "revision": (
            None
            if revision is None
            else {
                "id": str(revision.id),
                "node_id": str(revision.node_id),
                "revision_number": revision.revision_number,
                "title": revision.title,
                "body": revision.body,
                "tags": list(revision.tags),
                "conditions": list(revision.conditions),
                "exceptions": list(revision.exceptions),
                "content_hash": revision.content_hash,
                "edit_reason": revision.edit_reason,
            }
        ),
        "evidence_ids": [str(item) for item in evidence_ids],
    }
