from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    ActivityEvent,
    ActorType,
    CandidateAtomicity,
    CandidateEvidence,
    CandidateReview,
    CandidateReviewAction,
    CandidateReviewRequest,
    CandidateReviewResult,
    CandidateStatus,
    ExtractionCandidate,
    ExtractionJob,
    KnowledgeEvidence,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeRevision,
    OutboxEvent,
    ParseArtifactStatus,
    ReviewCard,
    SourceSection,
    SourceVersion,
)

TERMINAL_CANDIDATE_STATUSES = {
    CandidateStatus.ACCEPTED.value,
    CandidateStatus.REJECTED.value,
}
LOCAL_ACTOR = "local"


class CandidateSnapshot(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    suggested_destination_id: UUID | None = None
    conditions: list[str] | None = Field(default=None, max_length=12)
    exceptions: list[str] | None = Field(default=None, max_length=12)


class CandidateEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, min_length=1, max_length=20_000)
    tags: list[str] | None = Field(default=None, max_length=20)
    suggested_destination_id: UUID | None = None
    atomicity: CandidateAtomicity | None = None
    conditions: list[str] | None = Field(default=None, max_length=12)
    exceptions: list[str] | None = Field(default=None, max_length=12)
    evidence_section_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=12)

    @model_validator(mode="after")
    def require_change(self) -> CandidateEdit:
        mutable_fields = self.model_fields_set - {"expected_version"}
        has_destination_change = "suggested_destination_id" in mutable_fields
        has_value_change = any(
            getattr(self, field) is not None
            for field in mutable_fields - {"suggested_destination_id"}
        )
        if not has_destination_change and not has_value_change:
            raise ValueError("At least one candidate field must be supplied")
        return self


class CandidateAccept(CandidateSnapshot):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)


class CandidateNeedsVerification(CandidateSnapshot):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def reject_blank_reason(self) -> CandidateNeedsVerification:
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        return self


class CandidateReject(CandidateSnapshot):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def normalize_blank_reason(self) -> CandidateReject:
        if self.reason is not None and not self.reason.strip():
            self.reason = None
        return self


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    result_id: UUID
    candidate_review_id: UUID
    candidate_id: UUID
    candidate_version: int
    candidate_status: str
    knowledge_node_id: UUID | None
    knowledge_revision_id: UUID | None
    review_card_id: UUID | None
    evidence_ids: list[UUID]


@dataclass(frozen=True, slots=True)
class DestinationRecord:
    node: KnowledgeNode
    revision: KnowledgeRevision


@dataclass(frozen=True, slots=True)
class ValidatedEvidence:
    link: CandidateEvidence
    section: SourceSection


def review_request_hash(operation: CandidateReviewAction, request: BaseModel) -> str:
    payload = {
        "operation": operation.value,
        "request": request.model_dump(mode="json", exclude_unset=True),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CandidateReviewService:
    async def list_destinations(
        self, session: AsyncSession, *, space_id: UUID
    ) -> list[DestinationRecord]:
        rows = list(
            (
                await session.execute(
                    select(KnowledgeNode, KnowledgeRevision)
                    .join(
                        KnowledgeRevision,
                        KnowledgeRevision.id == KnowledgeNode.current_revision_id,
                    )
                    .where(
                        KnowledgeNode.space_id == space_id,
                        KnowledgeNode.kind == KnowledgeNodeKind.DOCUMENT.value,
                        KnowledgeNode.deleted_at.is_(None),
                    )
                    .order_by(KnowledgeRevision.title, KnowledgeNode.id)
                )
            ).all()
        )
        return [DestinationRecord(node, revision) for node, revision in rows]

    async def get_request(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        candidate_id: UUID,
        idempotency_key: str,
    ) -> tuple[CandidateReviewRequest, ReviewOutcome]:
        key = _validate_idempotency_key(idempotency_key)
        row = (
            await session.execute(
                select(CandidateReviewRequest, CandidateReviewResult)
                .join(
                    CandidateReviewResult,
                    CandidateReviewResult.request_id == CandidateReviewRequest.id,
                )
                .where(
                    CandidateReviewRequest.space_id == space_id,
                    CandidateReviewRequest.candidate_id == candidate_id,
                    CandidateReviewRequest.idempotency_key == key,
                )
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                "candidate_review_request_not_found",
                "Candidate review request was not found.",
                status_code=404,
            )
        request_row, result = row
        return request_row, self._outcome(result, candidate_id)

    async def edit(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        candidate_id: UUID,
        idempotency_key: str,
        request: CandidateEdit,
    ) -> ReviewOutcome:
        operation = CandidateReviewAction.EDIT
        request_hash = review_request_hash(operation, request)
        replay = await self._replay(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            key=_validate_idempotency_key(idempotency_key),
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        candidate = await self._lock_candidate(session, space_id, candidate_id)
        replay = await self._replay(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            key=idempotency_key.strip(),
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        self._validate_mutable(candidate, request.expected_version)
        before_snapshot = _candidate_snapshot(candidate)

        if "suggested_destination_id" in request.model_fields_set:
            if request.suggested_destination_id is not None:
                await self._destination(
                    session, space_id=space_id, destination_id=request.suggested_destination_id
                )
            candidate.suggested_destination_id = request.suggested_destination_id
        if request.title is not None:
            candidate.title = _required_text(request.title, "title")
        if request.body is not None:
            candidate.body = _required_text(request.body, "body")
        if request.tags is not None:
            candidate.tags = _clean_list(request.tags)
        if request.atomicity is not None:
            candidate.atomicity = request.atomicity.value
        if request.conditions is not None:
            candidate.conditions = _clean_list(request.conditions)
        if request.exceptions is not None:
            candidate.exceptions = _clean_list(request.exceptions)
        if request.evidence_section_ids is not None:
            evidence = await self._sections_for_edit(
                session, candidate=candidate, section_ids=request.evidence_section_ids
            )
            await session.execute(
                delete(CandidateEvidence).where(CandidateEvidence.candidate_id == candidate.id)
            )
            for section in evidence:
                session.add(
                    CandidateEvidence(
                        id=uuid4(),
                        candidate_id=candidate.id,
                        source_version_id=candidate.source_version_id,
                        section_id=section.id,
                        quote_hash=section.quote_hash,
                    )
                )
        return await self._record(
            session,
            candidate=candidate,
            operation=operation,
            actor=LOCAL_ACTOR,
            reason=None,
            to_status=candidate.status,
            key=idempotency_key.strip(),
            request_hash=request_hash,
            expected_version=request.expected_version,
            before_snapshot=before_snapshot,
        )

    async def accept(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        candidate_id: UUID,
        idempotency_key: str,
        request: CandidateAccept,
    ) -> ReviewOutcome:
        operation = CandidateReviewAction.ACCEPT
        key = _validate_idempotency_key(idempotency_key)
        request_hash = review_request_hash(operation, request)
        replay = await self._replay(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        candidate = await self._lock_candidate(session, space_id, candidate_id)
        replay = await self._replay(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        self._validate_mutable(candidate, request.expected_version)
        before_snapshot = _candidate_snapshot(candidate)
        self._apply_snapshot(candidate, request)
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(str(space_id))))
        )
        destination = await self._destination(
            session,
            space_id=space_id,
            destination_id=candidate.suggested_destination_id,
            for_update=True,
        )
        candidate.suggested_destination_id = destination.id
        evidence = await self._validated_evidence(session, candidate)
        source_version = await session.get(SourceVersion, candidate.source_version_id)
        if source_version is None:
            raise AppError(
                "candidate_evidence_stale",
                "Candidate source version is unavailable.",
                status_code=409,
            )
        await session.execute(
            select(KnowledgeNode.id)
            .where(KnowledgeNode.space_id == space_id)
            .with_for_update()
        )
        next_order = (
            await session.scalar(
                select(func.max(KnowledgeNode.sort_order)).where(
                    KnowledgeNode.space_id == space_id,
                    KnowledgeNode.parent_id == destination.id,
                    KnowledgeNode.deleted_at.is_(None),
                )
            )
            or -1
        ) + 1
        node_id = uuid4()

        node = KnowledgeNode(
            id=node_id,
            space_id=space_id,
            parent_id=destination.id,
            kind=KnowledgeNodeKind.POINT.value,
            origin_candidate_id=candidate.id,
            path=f"{destination.path}.n{node_id.hex}",
            version=1,
            sort_order=next_order,
        )
        session.add(node)
        await session.flush()
        revision = KnowledgeRevision(
            id=uuid4(),
            node_id=node.id,
            space_id=space_id,
            revision_number=1,
            title=candidate.title,
            body=candidate.body,
            tags=candidate.tags,
            conditions=candidate.conditions,
            exceptions=candidate.exceptions,
            actor=LOCAL_ACTOR,
            content_hash=_knowledge_content_hash(
                title=candidate.title,
                body=candidate.body,
                tags=candidate.tags,
                conditions=candidate.conditions,
                exceptions=candidate.exceptions,
            ),
            edit_reason="Accepted candidate",
        )
        session.add(revision)
        await session.flush()
        node.current_revision_id = revision.id
        card = ReviewCard(
            id=uuid4(),
            space_id=space_id,
            knowledge_node_id=node.id,
            knowledge_revision_id=revision.id,
            status="active",
        )
        evidence_rows = [
            KnowledgeEvidence(
                id=uuid4(),
                revision_id=revision.id,
                space_id=space_id,
                source_version_id=item.section.source_version_id,
                parse_artifact_id=item.section.parse_artifact_id,
                section_id=item.section.id,
                quote_hash=item.section.quote_hash,
                content_hash=item.section.content_hash,
                locator=item.section.locator,
                frozen_quote=item.section.text,
            )
            for item in evidence
        ]
        source_node = await session.scalar(
            select(KnowledgeNode).where(
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.parent_id == destination.id,
                KnowledgeNode.kind == KnowledgeNodeKind.SOURCE.value,
                KnowledgeNode.source_version_id == candidate.source_version_id,
                KnowledgeNode.deleted_at.is_(None),
            )
        )
        if source_node is None:
            source_node_id = uuid4()
            source_node = KnowledgeNode(
                id=source_node_id,
                space_id=space_id,
                parent_id=destination.id,
                kind=KnowledgeNodeKind.SOURCE.value,
                path=f"{destination.path}.n{source_node_id.hex}",
                version=1,
                sort_order=next_order + 1,
                source_id=source_version.source_id,
                source_version_id=source_version.id,
            )
        session.add_all([card, source_node, *evidence_rows])
        return await self._record(
            session,
            candidate=candidate,
            operation=operation,
            actor=LOCAL_ACTOR,
            reason=None,
            to_status=CandidateStatus.ACCEPTED.value,
            key=key,
            request_hash=request_hash,
            expected_version=request.expected_version,
            before_snapshot=before_snapshot,
            node=node,
            revision=revision,
            card=card,
            evidence=evidence_rows,
        )

    async def mark_needs_verification(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        candidate_id: UUID,
        idempotency_key: str,
        request: CandidateNeedsVerification,
    ) -> ReviewOutcome:
        return await self._status_change(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            operation=CandidateReviewAction.MARK_NEEDS_VERIFICATION,
            request=request,
            to_status=CandidateStatus.NEEDS_VERIFICATION.value,
            reason=request.reason.strip(),
        )

    async def reject(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        candidate_id: UUID,
        idempotency_key: str,
        request: CandidateReject,
    ) -> ReviewOutcome:
        return await self._status_change(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            operation=CandidateReviewAction.REJECT,
            request=request,
            to_status=CandidateStatus.REJECTED.value,
            reason=request.reason.strip() if request.reason else None,
        )

    async def _status_change(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        candidate_id: UUID,
        idempotency_key: str,
        operation: CandidateReviewAction,
        request: CandidateNeedsVerification | CandidateReject,
        to_status: str,
        reason: str | None,
    ) -> ReviewOutcome:
        key = _validate_idempotency_key(idempotency_key)
        request_hash = review_request_hash(operation, request)
        replay = await self._replay(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        candidate = await self._lock_candidate(session, space_id, candidate_id)
        replay = await self._replay(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        self._validate_mutable(candidate, request.expected_version)
        before_snapshot = _candidate_snapshot(candidate)
        self._apply_snapshot(candidate, request)
        candidate.verification_reason = (
            reason if to_status == CandidateStatus.NEEDS_VERIFICATION.value else None
        )
        candidate.rejection_reason = (
            reason if to_status == CandidateStatus.REJECTED.value else None
        )
        return await self._record(
            session,
            candidate=candidate,
            operation=operation,
            actor=LOCAL_ACTOR,
            reason=reason,
            to_status=to_status,
            key=key,
            request_hash=request_hash,
            expected_version=request.expected_version,
            before_snapshot=before_snapshot,
        )

    async def _record(
        self,
        session: AsyncSession,
        *,
        candidate: ExtractionCandidate,
        operation: CandidateReviewAction,
        actor: str,
        reason: str | None,
        to_status: str,
        key: str,
        request_hash: str,
        expected_version: int,
        before_snapshot: dict[str, Any] | None = None,
        node: KnowledgeNode | None = None,
        revision: KnowledgeRevision | None = None,
        card: ReviewCard | None = None,
        evidence: list[KnowledgeEvidence] | None = None,
    ) -> ReviewOutcome:
        from_status = candidate.status
        from_version = candidate.version
        before = before_snapshot or _candidate_snapshot(candidate)
        candidate.status = to_status
        candidate.version += 1
        if operation != CandidateReviewAction.EDIT:
            candidate.reviewed_at = datetime.now(UTC)
        if to_status != CandidateStatus.REJECTED.value:
            candidate.rejection_reason = None
        if to_status != CandidateStatus.NEEDS_VERIFICATION.value:
            candidate.verification_reason = None
        request_row = CandidateReviewRequest(
            id=uuid4(),
            candidate_id=candidate.id,
            space_id=candidate.space_id,
            idempotency_key=key,
            operation=operation.value,
            request_hash=request_hash,
            expected_version=expected_version,
        )
        review = CandidateReview(
            id=uuid4(),
            candidate_id=candidate.id,
            space_id=candidate.space_id,
            action=operation.value,
            actor=actor,
            reason=reason,
            before_snapshot=before,
            after_snapshot=_candidate_snapshot(candidate),
            from_status=from_status,
            to_status=to_status,
            from_version=from_version,
            to_version=candidate.version,
        )
        evidence_ids = [item.id for item in evidence or []]
        result = CandidateReviewResult(
            id=uuid4(),
            request_id=request_row.id,
            candidate_review_id=review.id,
            candidate_version=candidate.version,
            candidate_status=to_status,
            knowledge_node_id=node.id if node else None,
            knowledge_revision_id=revision.id if revision else None,
            review_card_id=card.id if card else None,
            evidence_ids=[str(item) for item in evidence_ids],
        )
        event_suffix = operation.value
        payload: dict[str, Any] = {
            "candidate_id": str(candidate.id),
            "candidate_review_id": str(review.id),
            "candidate_version": candidate.version,
            "status": to_status,
            "actor": actor,
        }
        if node is not None:
            payload.update(
                {
                    "knowledge_node_id": str(node.id),
                    "knowledge_revision_id": str(revision.id) if revision else None,
                    "review_card_id": str(card.id) if card else None,
                }
            )
        session.add_all(
            [
                request_row,
                review,
                result,
                ActivityEvent(
                    id=uuid4(),
                    space_id=candidate.space_id,
                    event_type=f"candidate.{event_suffix}",
                    entity_type="extraction_candidate",
                    entity_id=candidate.id,
                    actor_type=ActorType.USER.value,
                    payload=payload,
                ),
                OutboxEvent(
                    id=uuid4(),
                    space_id=candidate.space_id,
                    aggregate_type="extraction_candidate",
                    aggregate_id=candidate.id,
                    event_type=f"candidate.{event_suffix}",
                    deduplication_key=f"candidate-review:{request_row.id}",
                    payload=payload,
                ),
            ]
        )
        await session.flush()
        return self._outcome(result, candidate.id)

    async def _replay(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        candidate_id: UUID,
        key: str,
        request_hash: str,
    ) -> ReviewOutcome | None:
        row = (
            await session.execute(
                select(CandidateReviewRequest, CandidateReviewResult)
                .join(
                    CandidateReviewResult,
                    CandidateReviewResult.request_id == CandidateReviewRequest.id,
                )
                .where(
                    CandidateReviewRequest.space_id == space_id,
                    CandidateReviewRequest.candidate_id == candidate_id,
                    CandidateReviewRequest.idempotency_key == key,
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
        return self._outcome(result, candidate_id)

    @staticmethod
    def _outcome(result: CandidateReviewResult, candidate_id: UUID) -> ReviewOutcome:
        return ReviewOutcome(
            result_id=result.id,
            candidate_review_id=result.candidate_review_id,
            candidate_id=candidate_id,
            candidate_version=result.candidate_version,
            candidate_status=result.candidate_status,
            knowledge_node_id=result.knowledge_node_id,
            knowledge_revision_id=result.knowledge_revision_id,
            review_card_id=result.review_card_id,
            evidence_ids=[UUID(item) for item in result.evidence_ids],
        )

    @staticmethod
    async def _lock_candidate(
        session: AsyncSession, space_id: UUID, candidate_id: UUID
    ) -> ExtractionCandidate:
        candidate = await session.scalar(
            select(ExtractionCandidate)
            .where(
                ExtractionCandidate.id == candidate_id,
                ExtractionCandidate.space_id == space_id,
            )
            .with_for_update()
        )
        if candidate is None:
            raise AppError("candidate_not_found", "Candidate was not found.", status_code=404)
        return candidate

    @staticmethod
    def _validate_mutable(candidate: ExtractionCandidate, expected_version: int) -> None:
        if candidate.version != expected_version:
            raise AppError(
                "candidate_version_conflict",
                "Candidate version does not match expected_version.",
                status_code=409,
                details=[
                    {
                        "expected_version": expected_version,
                        "actual_version": candidate.version,
                    }
                ],
            )
        if candidate.status in TERMINAL_CANDIDATE_STATUSES:
            raise AppError(
                "candidate_terminal",
                "Accepted and rejected candidates cannot be changed.",
                status_code=409,
            )

    @staticmethod
    def _apply_snapshot(
        candidate: ExtractionCandidate, request: CandidateSnapshot
    ) -> None:
        candidate.title = _required_text(request.title, "title")
        candidate.body = _required_text(request.body, "body")
        candidate.tags = _clean_list(request.tags)
        candidate.suggested_destination_id = request.suggested_destination_id
        if request.conditions is not None:
            candidate.conditions = _clean_list(request.conditions)
        if request.exceptions is not None:
            candidate.exceptions = _clean_list(request.exceptions)

    @staticmethod
    async def _destination(
        session: AsyncSession,
        *,
        space_id: UUID,
        destination_id: UUID | None,
        for_update: bool = False,
    ) -> KnowledgeNode:
        if destination_id is None:
            statement = (
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.space_id == space_id,
                    KnowledgeNode.kind == KnowledgeNodeKind.DOCUMENT.value,
                    KnowledgeNode.deleted_at.is_(None),
                )
                .order_by(KnowledgeNode.created_at, KnowledgeNode.id)
                .limit(1)
            )
        else:
            statement = select(KnowledgeNode).where(
                KnowledgeNode.id == destination_id,
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.kind == KnowledgeNodeKind.DOCUMENT.value,
                KnowledgeNode.deleted_at.is_(None),
            )
        if for_update:
            statement = statement.with_for_update()
        destination = await session.scalar(statement)
        if destination is None:
            raise AppError(
                "destination_not_found",
                "A valid destination in this knowledge space is required.",
                status_code=404,
            )
        return destination

    @staticmethod
    async def _sections_for_edit(
        session: AsyncSession,
        *,
        candidate: ExtractionCandidate,
        section_ids: list[UUID],
    ) -> list[SourceSection]:
        unique_ids = list(dict.fromkeys(section_ids))
        extraction = await session.get(ExtractionJob, candidate.extraction_job_id)
        if extraction is None:
            raise AppError(
                "invalid_candidate_evidence",
                "The candidate extraction record is unavailable.",
                status_code=409,
            )
        sections = list(
            await session.scalars(
                select(SourceSection).where(
                    SourceSection.id.in_(unique_ids),
                    SourceSection.space_id == candidate.space_id,
                    SourceSection.source_version_id == candidate.source_version_id,
                    SourceSection.parse_artifact_id == extraction.parse_artifact_id,
                )
            )
        )
        if len(sections) != len(unique_ids):
            raise AppError(
                "invalid_candidate_evidence",
                "Evidence must belong to the candidate extraction artifact and space.",
                status_code=409,
            )
        return sections

    @staticmethod
    async def _validated_evidence(
        session: AsyncSession, candidate: ExtractionCandidate
    ) -> list[ValidatedEvidence]:
        extraction = await session.get(ExtractionJob, candidate.extraction_job_id)
        version = await session.get(SourceVersion, candidate.source_version_id)
        if (
            extraction is None
            or version is None
            or extraction.space_id != candidate.space_id
            or extraction.source_version_id != candidate.source_version_id
            or version.current_parse_artifact_id != extraction.parse_artifact_id
            or version.parse_status != ParseArtifactStatus.READY.value
        ):
            raise AppError(
                "candidate_evidence_stale",
                "Candidate evidence no longer points to the current ready extraction source.",
                status_code=409,
            )
        rows = list(
            (
                await session.execute(
                    select(CandidateEvidence, SourceSection)
                    .join(SourceSection, SourceSection.id == CandidateEvidence.section_id)
                    .where(CandidateEvidence.candidate_id == candidate.id)
                    .order_by(SourceSection.ordinal)
                )
            ).all()
        )
        if not rows:
            raise AppError(
                "candidate_evidence_missing",
                "At least one valid evidence section is required for acceptance.",
                status_code=409,
            )
        validated: list[ValidatedEvidence] = []
        for link, section in rows:
            locator = section.locator
            valid = (
                link.source_version_id == candidate.source_version_id
                and link.quote_hash == section.quote_hash
                and section.space_id == candidate.space_id
                and section.source_version_id == candidate.source_version_id
                and section.parse_artifact_id == extraction.parse_artifact_id
                and locator.get("sourceVersionId") == str(candidate.source_version_id)
                and locator.get("parseArtifactId") == str(extraction.parse_artifact_id)
                and locator.get("sectionId") == str(section.id)
                and locator.get("quoteHash") == section.quote_hash
            )
            if not valid:
                raise AppError(
                    "candidate_evidence_stale",
                    "Candidate evidence identity or hash validation failed.",
                    status_code=409,
                )
            validated.append(ValidatedEvidence(link, section))
        return validated


def _knowledge_content_hash(
    *,
    title: str,
    body: str,
    tags: list[str],
    conditions: list[str],
    exceptions: list[str],
) -> str:
    payload = {
        "title": title,
        "body": body,
        "tags": tags,
        "conditions": conditions,
        "exceptions": exceptions,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _candidate_snapshot(candidate: ExtractionCandidate) -> dict[str, Any]:
    return {
        "title": candidate.title,
        "body": candidate.body,
        "tags": list(candidate.tags),
        "suggested_destination_id": (
            str(candidate.suggested_destination_id)
            if candidate.suggested_destination_id is not None
            else None
        ),
        "atomicity": candidate.atomicity,
        "conditions": list(candidate.conditions),
        "exceptions": list(candidate.exceptions),
        "status": candidate.status,
        "verification_reason": candidate.verification_reason,
        "rejection_reason": candidate.rejection_reason,
        "version": candidate.version,
    }


def _required_text(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise AppError(
            "invalid_candidate_payload",
            f"{field} must not be blank.",
            status_code=422,
        )
    return cleaned


def _clean_list(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))
