from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    ActivityEvent,
    ActorType,
    KnowledgeEvidence,
    KnowledgeNode,
    KnowledgeRevision,
    KnowledgeUpdateAction,
    KnowledgeUpdateProposal,
    KnowledgeUpdateProposalEvidence,
    KnowledgeUpdateProposalEvidenceRole,
    KnowledgeUpdateProposalOperation,
    KnowledgeUpdateProposalRequest,
    KnowledgeUpdateProposalResult,
    KnowledgeUpdateProposalStatus,
    KnowledgeUpdateProposalTransition,
    OutboxEvent,
    ParseArtifactStatus,
    ParseStatus,
    ResearchRun,
    Source,
    SourceParseArtifact,
    SourceSection,
    SourceVersion,
)

LOCAL_ACTOR = "local"
TERMINAL_STATUSES = {
    KnowledgeUpdateProposalStatus.REJECTED.value,
    KnowledgeUpdateProposalStatus.APPLIED.value,
    KnowledgeUpdateProposalStatus.SUPERSEDED.value,
}
REVISION_EVIDENCE_ROLES = {
    KnowledgeUpdateProposalEvidenceRole.EXISTING_SUPPORT.value,
    KnowledgeUpdateProposalEvidenceRole.CONFLICT.value,
    KnowledgeUpdateProposalEvidenceRole.OUTDATED.value,
}


class ProposalEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: KnowledgeUpdateProposalEvidenceRole
    section_id: UUID
    knowledge_revision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_revision_reference(self) -> ProposalEvidenceInput:
        if (
            self.role.value not in REVISION_EVIDENCE_ROLES
            and self.knowledge_revision_id is not None
        ):
            raise ValueError("This evidence role cannot target a knowledge revision")
        if self.role.value in REVISION_EVIDENCE_ROLES and self.knowledge_revision_id is None:
            raise ValueError("This evidence role requires a knowledge revision")
        return self


class ProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: KnowledgeUpdateAction
    research_run_id: UUID | None = None
    target_node_id: UUID | None = None
    target_revision_id: UUID | None = None
    suggested_title: str | None = Field(default=None, min_length=1, max_length=500)
    suggested_body: str | None = Field(default=None, min_length=1, max_length=20_000)
    suggested_tags: list[str] = Field(default_factory=list, max_length=20)
    conditions: list[str] = Field(default_factory=list, max_length=12)
    exceptions: list[str] = Field(default_factory=list, max_length=12)
    comparison_summary: str | None = Field(default=None, max_length=20_000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty_reason: str | None = Field(default=None, max_length=2000)
    evidence: list[ProposalEvidenceInput] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_target(self) -> ProposalCreate:
        if self.action is KnowledgeUpdateAction.CREATE:
            if self.target_revision_id is not None:
                raise ValueError("create proposals cannot target an existing revision")
        elif self.target_node_id is None or self.target_revision_id is None:
            raise ValueError("this proposal action requires target_node_id and target_revision_id")
        return self


class ProposalEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)
    suggested_title: str | None = Field(default=None, min_length=1, max_length=500)
    suggested_body: str | None = Field(default=None, min_length=1, max_length=20_000)
    suggested_tags: list[str] | None = Field(default=None, max_length=20)
    conditions: list[str] | None = Field(default=None, max_length=12)
    exceptions: list[str] | None = Field(default=None, max_length=12)
    comparison_summary: str | None = Field(default=None, max_length=20_000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_change(self) -> ProposalEdit:
        if not (self.model_fields_set - {"expected_version"}):
            raise ValueError("At least one proposal field must be supplied")
        return self


class ProposalSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def normalize_reason(self) -> ProposalDecision:
        if self.reason is not None and not self.reason.strip():
            self.reason = None
        return self


class ProposalSupersede(ProposalDecision):
    replacement_proposal_id: UUID


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    result_id: UUID
    transition_id: UUID
    proposal_id: UUID
    proposal_version: int
    proposal_status: str
    snapshot: dict[str, Any]


def proposal_request_hash(
    operation: KnowledgeUpdateProposalOperation,
    request: BaseModel,
    *,
    proposal_id: UUID | None = None,
) -> str:
    payload = {
        "operation": operation.value,
        "request": request.model_dump(mode="json", exclude_unset=True),
    }
    if proposal_id is not None:
        payload["proposal_id"] = str(proposal_id)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class KnowledgeUpdateProposalService:
    async def create(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        idempotency_key: str,
        request: ProposalCreate,
    ) -> ProposalOutcome:
        operation = KnowledgeUpdateProposalOperation.CREATE
        key = _validate_idempotency_key(idempotency_key)
        await self._lock_idempotency_key(session, space_id=space_id, key=key)
        request_hash = proposal_request_hash(operation, request)
        replay = await self._replay(session, space_id=space_id, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(space_id)))))
        replay = await self._replay(session, space_id=space_id, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        target = await self._target(
            session,
            space_id=space_id,
            action=request.action.value,
            node_id=request.target_node_id,
            revision_id=request.target_revision_id,
            for_update=True,
        )
        await self._research_run(
            session, space_id=space_id, research_run_id=request.research_run_id
        )
        proposal = KnowledgeUpdateProposal(
            id=uuid4(),
            space_id=space_id,
            research_run_id=request.research_run_id,
            target_node_id=request.target_node_id,
            target_revision_id=request.target_revision_id,
            target_node_version=target[0].version if target else None,
            action=request.action.value,
            status=KnowledgeUpdateProposalStatus.DRAFT.value,
            version=1,
            suggested_title=_optional_text(request.suggested_title),
            suggested_body=_optional_text(request.suggested_body),
            suggested_tags=_clean_list(request.suggested_tags),
            conditions=_clean_list(request.conditions),
            exceptions=_clean_list(request.exceptions),
            comparison_summary=_optional_text(request.comparison_summary),
            confidence=request.confidence,
            uncertainty_reason=_optional_text(request.uncertainty_reason),
        )
        evidence = await self._freeze_evidence(
            session, space_id=space_id, proposal=proposal, inputs=request.evidence
        )
        session.add_all([proposal, *evidence])
        return await self._record(
            session,
            proposal=proposal,
            operation=operation,
            key=key,
            request_hash=request_hash,
            expected_version=None,
            before_snapshot={},
            from_status="draft",
            from_version=0,
            reason=None,
        )

    async def create_comparison_draft(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        comparison_run_id: UUID,
        request: ProposalCreate,
    ) -> ProposalOutcome:
        """Create a draft from candidates already validated by the comparison service."""
        key = f"proposal-comparison:{comparison_run_id}"
        operation = KnowledgeUpdateProposalOperation.CREATE
        request_hash = hashlib.sha256(
            f"proposal-comparison:{comparison_run_id}".encode()
        ).hexdigest()
        await self._lock_idempotency_key(session, space_id=space_id, key=key)
        replay = await self._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        target = await self._target(
            session,
            space_id=space_id,
            action=request.action.value,
            node_id=request.target_node_id,
            revision_id=request.target_revision_id,
            for_update=True,
        )
        await self._research_run(
            session, space_id=space_id, research_run_id=request.research_run_id
        )
        proposal = KnowledgeUpdateProposal(
            id=uuid4(),
            space_id=space_id,
            research_run_id=request.research_run_id,
            target_node_id=request.target_node_id,
            target_revision_id=request.target_revision_id,
            target_node_version=target[0].version if target else None,
            action=request.action.value,
            status=KnowledgeUpdateProposalStatus.DRAFT.value,
            version=1,
            suggested_title=_optional_text(request.suggested_title),
            suggested_body=_optional_text(request.suggested_body),
            suggested_tags=_clean_list(request.suggested_tags),
            conditions=_clean_list(request.conditions),
            exceptions=_clean_list(request.exceptions),
            comparison_summary=_optional_text(request.comparison_summary),
            confidence=request.confidence,
            uncertainty_reason=_optional_text(request.uncertainty_reason),
        )
        evidence = await self._freeze_evidence(
            session, space_id=space_id, proposal=proposal, inputs=request.evidence
        )
        session.add_all([proposal, *evidence])
        return await self._record(
            session,
            proposal=proposal,
            operation=operation,
            key=key,
            request_hash=request_hash,
            expected_version=None,
            before_snapshot={},
            from_status="draft",
            from_version=0,
            reason=None,
        )

    async def edit(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        proposal_id: UUID,
        idempotency_key: str,
        request: ProposalEdit,
    ) -> ProposalOutcome:
        proposal = await self._prepare_change(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            operation=KnowledgeUpdateProposalOperation.EDIT,
            request=request,
        )
        if isinstance(proposal, ProposalOutcome):
            return proposal
        self._ensure_status(
            proposal, {KnowledgeUpdateProposalStatus.DRAFT.value}, request.expected_version
        )
        before = _proposal_snapshot(proposal)
        for field in (
            "suggested_title",
            "suggested_body",
            "comparison_summary",
            "uncertainty_reason",
        ):
            if field in request.model_fields_set:
                setattr(proposal, field, _optional_text(getattr(request, field)))
        for field in ("suggested_tags", "conditions", "exceptions"):
            value = getattr(request, field)
            if value is not None:
                setattr(proposal, field, _clean_list(value))
        if "confidence" in request.model_fields_set:
            proposal.confidence = request.confidence
        return await self._record(
            session,
            proposal=proposal,
            operation=KnowledgeUpdateProposalOperation.EDIT,
            key=idempotency_key.strip(),
            request_hash=proposal_request_hash(
                KnowledgeUpdateProposalOperation.EDIT,
                request,
                proposal_id=proposal.id,
            ),
            expected_version=request.expected_version,
            before_snapshot=before,
            from_status=proposal.status,
            from_version=proposal.version,
            reason=None,
        )

    async def submit(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        proposal_id: UUID,
        idempotency_key: str,
        request: ProposalSubmit,
    ) -> ProposalOutcome:
        return await self._transition(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            operation=KnowledgeUpdateProposalOperation.SUBMIT,
            request=request,
            allowed={KnowledgeUpdateProposalStatus.DRAFT.value},
            target_status=KnowledgeUpdateProposalStatus.PENDING_REVIEW.value,
            reason=None,
        )

    async def approve(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        proposal_id: UUID,
        idempotency_key: str,
        request: ProposalDecision,
    ) -> ProposalOutcome:
        return await self._transition(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            operation=KnowledgeUpdateProposalOperation.APPROVE,
            request=request,
            allowed={KnowledgeUpdateProposalStatus.PENDING_REVIEW.value},
            target_status=KnowledgeUpdateProposalStatus.APPROVED.value,
            reason=request.reason,
        )

    async def reject(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        proposal_id: UUID,
        idempotency_key: str,
        request: ProposalDecision,
    ) -> ProposalOutcome:
        return await self._transition(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            operation=KnowledgeUpdateProposalOperation.REJECT,
            request=request,
            allowed={KnowledgeUpdateProposalStatus.PENDING_REVIEW.value},
            target_status=KnowledgeUpdateProposalStatus.REJECTED.value,
            reason=request.reason,
        )

    async def supersede(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        proposal_id: UUID,
        idempotency_key: str,
        request: ProposalSupersede,
    ) -> ProposalOutcome:
        key = _validate_idempotency_key(idempotency_key)
        await self._lock_idempotency_key(session, space_id=space_id, key=key)
        operation = KnowledgeUpdateProposalOperation.SUPERSEDE
        request_hash = proposal_request_hash(operation, request, proposal_id=proposal_id)
        replay = await self._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=request_hash,
            proposal_id=proposal_id,
        )
        if replay is not None:
            return replay
        ids = sorted((proposal_id, request.replacement_proposal_id), key=str)
        locked = list(
            await session.scalars(
                select(KnowledgeUpdateProposal)
                .where(
                    KnowledgeUpdateProposal.space_id == space_id,
                    KnowledgeUpdateProposal.id.in_(ids),
                )
                .order_by(KnowledgeUpdateProposal.id)
                .with_for_update()
            )
        )
        if len(locked) != 2:
            raise AppError(
                "proposal_replacement_invalid",
                "Replacement proposal was not found.",
                status_code=409,
            )
        proposal = next(item for item in locked if item.id == proposal_id)
        replacement = next(item for item in locked if item.id == request.replacement_proposal_id)
        replay = await self._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=request_hash,
            proposal_id=proposal_id,
        )
        if replay is not None:
            return replay
        self._ensure_status(
            proposal,
            {
                KnowledgeUpdateProposalStatus.DRAFT.value,
                KnowledgeUpdateProposalStatus.PENDING_REVIEW.value,
                KnowledgeUpdateProposalStatus.APPROVED.value,
            },
            request.expected_version,
        )
        if replacement.status in TERMINAL_STATUSES:
            raise AppError(
                "proposal_replacement_invalid", "Replacement proposal is terminal.", status_code=409
            )
        before = _proposal_snapshot(proposal)
        proposal.superseded_by_proposal_id = replacement.id
        proposal.superseded_at = datetime.now(UTC)
        return await self._record(
            session,
            proposal=proposal,
            operation=operation,
            key=key,
            request_hash=request_hash,
            expected_version=request.expected_version,
            before_snapshot=before,
            from_status=proposal.status,
            from_version=proposal.version,
            reason=request.reason,
            to_status=KnowledgeUpdateProposalStatus.SUPERSEDED.value,
        )

    async def get(
        self, session: AsyncSession, *, space_id: UUID, proposal_id: UUID
    ) -> ProposalOutcome:
        proposal = await self._lock_proposal(session, space_id, proposal_id, for_update=False)
        result = await session.scalar(
            select(KnowledgeUpdateProposalResult)
            .where(KnowledgeUpdateProposalResult.proposal_id == proposal.id)
            .order_by(KnowledgeUpdateProposalResult.created_at.desc())
            .limit(1)
        )
        if result is None:
            raise AppError("proposal_not_found", "Proposal result was not found.", status_code=404)
        return self._outcome(result)

    async def get_record(
        self, session: AsyncSession, *, space_id: UUID, proposal_id: UUID
    ) -> tuple[
        KnowledgeUpdateProposal,
        list[KnowledgeUpdateProposalEvidence],
        list[KnowledgeUpdateProposalTransition],
    ]:
        proposal = await self._lock_proposal(session, space_id, proposal_id, for_update=False)
        return (
            proposal,
            await self.evidence(session, proposal_id=proposal.id),
            await self.transitions(session, proposal_id=proposal.id),
        )

    async def list_proposals(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        status: str | None = None,
        action: str | None = None,
        target_node_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[KnowledgeUpdateProposal]:
        statement = select(KnowledgeUpdateProposal).where(
            KnowledgeUpdateProposal.space_id == space_id
        )
        if status is not None:
            statement = statement.where(KnowledgeUpdateProposal.status == status)
        if action is not None:
            statement = statement.where(KnowledgeUpdateProposal.action == action)
        if target_node_id is not None:
            statement = statement.where(KnowledgeUpdateProposal.target_node_id == target_node_id)
        return list(
            await session.scalars(
                statement.order_by(KnowledgeUpdateProposal.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )

    async def evidence(
        self, session: AsyncSession, *, proposal_id: UUID
    ) -> list[KnowledgeUpdateProposalEvidence]:
        return list(
            await session.scalars(
                select(KnowledgeUpdateProposalEvidence)
                .where(KnowledgeUpdateProposalEvidence.proposal_id == proposal_id)
                .order_by(KnowledgeUpdateProposalEvidence.ordinal)
            )
        )

    async def transitions(
        self, session: AsyncSession, *, proposal_id: UUID
    ) -> list[KnowledgeUpdateProposalTransition]:
        return list(
            await session.scalars(
                select(KnowledgeUpdateProposalTransition)
                .where(KnowledgeUpdateProposalTransition.proposal_id == proposal_id)
                .order_by(KnowledgeUpdateProposalTransition.to_version)
            )
        )

    async def get_request(
        self, session: AsyncSession, *, space_id: UUID, idempotency_key: str
    ) -> tuple[KnowledgeUpdateProposalRequest, ProposalOutcome]:
        key = _validate_idempotency_key(idempotency_key)
        row = await session.execute(
            select(KnowledgeUpdateProposalRequest, KnowledgeUpdateProposalResult)
            .join(
                KnowledgeUpdateProposalResult,
                KnowledgeUpdateProposalResult.request_id == KnowledgeUpdateProposalRequest.id,
            )
            .where(
                KnowledgeUpdateProposalRequest.space_id == space_id,
                KnowledgeUpdateProposalRequest.idempotency_key == key,
            )
        )
        stored = row.one_or_none()
        if stored is None:
            raise AppError(
                "proposal_request_not_found", "Proposal request was not found.", status_code=404
            )
        request_row, result = stored
        return request_row, self._outcome(result)

    async def _transition(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        proposal_id: UUID,
        idempotency_key: str,
        operation: KnowledgeUpdateProposalOperation,
        request: ProposalSubmit | ProposalDecision,
        allowed: set[str],
        target_status: str,
        reason: str | None,
    ) -> ProposalOutcome:
        proposal = await self._prepare_change(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            operation=operation,
            request=request,
        )
        if isinstance(proposal, ProposalOutcome):
            return proposal
        self._ensure_status(proposal, allowed, request.expected_version)
        before = _proposal_snapshot(proposal)
        if operation is KnowledgeUpdateProposalOperation.APPROVE:
            target = await self._target(
                session,
                space_id=space_id,
                action=proposal.action,
                node_id=proposal.target_node_id,
                revision_id=proposal.target_revision_id,
                for_update=True,
            )
            if target is not None:
                node, revision = target
                if (
                    proposal.target_node_version is not None
                    and node.version != proposal.target_node_version
                ):
                    raise AppError(
                        "proposal_target_stale",
                        "Proposal target knowledge version is stale.",
                        status_code=409,
                    )
                if revision is not None and node.current_revision_id != revision.id:
                    raise AppError(
                        "proposal_target_stale",
                        "Proposal target revision is no longer current.",
                        status_code=409,
                    )
            await self._validate_frozen_evidence(session, proposal)
            proposal.reviewed_at = datetime.now(UTC)
        elif operation is KnowledgeUpdateProposalOperation.REJECT:
            proposal.reviewed_at = datetime.now(UTC)
        elif operation is KnowledgeUpdateProposalOperation.SUBMIT:
            proposal.submitted_at = datetime.now(UTC)
        return await self._record(
            session,
            proposal=proposal,
            operation=operation,
            key=idempotency_key.strip(),
            request_hash=proposal_request_hash(
                operation,
                request,
                proposal_id=proposal.id,
            ),
            expected_version=request.expected_version,
            before_snapshot=before,
            from_status=proposal.status,
            from_version=proposal.version,
            reason=reason,
            to_status=target_status,
        )

    async def _prepare_change(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        proposal_id: UUID,
        idempotency_key: str,
        operation: KnowledgeUpdateProposalOperation,
        request: BaseModel,
    ) -> KnowledgeUpdateProposal | ProposalOutcome:
        key = _validate_idempotency_key(idempotency_key)
        await self._lock_idempotency_key(session, space_id=space_id, key=key)
        request_hash = proposal_request_hash(operation, request, proposal_id=proposal_id)
        replay = await self._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=request_hash,
            proposal_id=proposal_id,
        )
        if replay is not None:
            return replay
        proposal = await self._lock_proposal(session, space_id, proposal_id, for_update=True)
        replay = await self._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=request_hash,
            proposal_id=proposal.id,
        )
        return replay if replay is not None else proposal

    async def _record(
        self,
        session: AsyncSession,
        *,
        proposal: KnowledgeUpdateProposal,
        operation: KnowledgeUpdateProposalOperation,
        key: str,
        request_hash: str,
        expected_version: int | None,
        before_snapshot: dict[str, Any],
        from_status: str,
        from_version: int,
        reason: str | None,
        to_status: str | None = None,
    ) -> ProposalOutcome:
        final_status = to_status or proposal.status
        proposal.status = final_status
        proposal.version = from_version + 1
        request_row = KnowledgeUpdateProposalRequest(
            id=uuid4(),
            space_id=proposal.space_id,
            proposal_id=proposal.id,
            idempotency_key=key,
            operation=operation.value,
            request_hash=request_hash,
            expected_version=expected_version,
        )
        transition = KnowledgeUpdateProposalTransition(
            id=uuid4(),
            proposal_id=proposal.id,
            space_id=proposal.space_id,
            request_id=request_row.id,
            operation=operation.value,
            actor=LOCAL_ACTOR,
            reason=reason,
            before_snapshot=before_snapshot,
            after_snapshot=_proposal_snapshot(proposal),
            from_status=from_status,
            to_status=final_status,
            from_version=from_version,
            to_version=proposal.version,
        )
        result = KnowledgeUpdateProposalResult(
            id=uuid4(),
            request_id=request_row.id,
            proposal_id=proposal.id,
            transition_id=transition.id,
            space_id=proposal.space_id,
            proposal_version=proposal.version,
            proposal_status=final_status,
            snapshot=_proposal_snapshot(proposal),
        )
        payload = {
            "proposal_id": str(proposal.id),
            "transition_id": str(transition.id),
            "proposal_version": proposal.version,
            "status": final_status,
            "actor": LOCAL_ACTOR,
        }
        session.add(request_row)
        await session.flush()
        session.add(transition)
        await session.flush()
        session.add_all(
            [
                result,
                ActivityEvent(
                    id=uuid4(),
                    space_id=proposal.space_id,
                    event_type=f"proposal.{operation.value}",
                    entity_type="knowledge_update_proposal",
                    entity_id=proposal.id,
                    actor_type=ActorType.USER.value,
                    payload=payload,
                ),
                OutboxEvent(
                    id=uuid4(),
                    space_id=proposal.space_id,
                    aggregate_type="knowledge_update_proposal",
                    aggregate_id=proposal.id,
                    event_type=f"proposal.{operation.value}",
                    deduplication_key=f"proposal-transition:{transition.id}",
                    payload=payload,
                ),
            ]
        )
        await session.flush()
        return self._outcome(result)

    async def _replay(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        key: str,
        request_hash: str,
        proposal_id: UUID | None = None,
    ) -> ProposalOutcome | None:
        row = await session.execute(
            select(KnowledgeUpdateProposalRequest, KnowledgeUpdateProposalResult)
            .join(
                KnowledgeUpdateProposalResult,
                KnowledgeUpdateProposalResult.request_id == KnowledgeUpdateProposalRequest.id,
            )
            .where(
                KnowledgeUpdateProposalRequest.space_id == space_id,
                KnowledgeUpdateProposalRequest.idempotency_key == key,
            )
        )
        stored = row.one_or_none()
        if stored is None:
            return None
        request_row, result = stored
        if request_row.request_hash != request_hash or (
            proposal_id is not None and request_row.proposal_id != proposal_id
        ):
            raise AppError(
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request.",
                status_code=409,
            )
        return self._outcome(result)

    @staticmethod
    def _outcome(result: KnowledgeUpdateProposalResult) -> ProposalOutcome:
        return ProposalOutcome(
            result_id=result.id,
            transition_id=result.transition_id,
            proposal_id=result.proposal_id,
            proposal_version=result.proposal_version,
            proposal_status=result.proposal_status,
            snapshot=dict(result.snapshot),
        )

    @staticmethod
    async def _lock_idempotency_key(session: AsyncSession, *, space_id: UUID, key: str) -> None:
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(f"proposal:{space_id}:{key}")))
        )

    @staticmethod
    async def _research_run(
        session: AsyncSession, *, space_id: UUID, research_run_id: UUID | None
    ) -> None:
        if research_run_id is None:
            return
        exists = await session.scalar(
            select(ResearchRun.id).where(
                ResearchRun.id == research_run_id,
                ResearchRun.space_id == space_id,
            )
        )
        if exists is None:
            raise AppError(
                "invalid_proposal_payload",
                "Research run is outside this knowledge space.",
                status_code=422,
            )

    @staticmethod
    async def _lock_proposal(
        session: AsyncSession, space_id: UUID, proposal_id: UUID, *, for_update: bool
    ) -> KnowledgeUpdateProposal:
        statement = select(KnowledgeUpdateProposal).where(
            KnowledgeUpdateProposal.id == proposal_id, KnowledgeUpdateProposal.space_id == space_id
        )
        if for_update:
            statement = statement.with_for_update()
        proposal = await session.scalar(statement)
        if proposal is None:
            raise AppError("proposal_not_found", "Proposal was not found.", status_code=404)
        return proposal

    @staticmethod
    def _ensure_status(
        proposal: KnowledgeUpdateProposal, allowed: set[str], expected_version: int
    ) -> None:
        if proposal.version != expected_version:
            raise AppError(
                "proposal_version_conflict",
                "Proposal version does not match expected_version.",
                status_code=409,
                details=[
                    {"expected_version": expected_version, "actual_version": proposal.version}
                ],
            )
        if proposal.status in TERMINAL_STATUSES:
            raise AppError(
                "proposal_terminal", "Terminal proposals cannot be changed.", status_code=409
            )
        if proposal.status not in allowed:
            raise AppError(
                "proposal_invalid_transition",
                "Proposal status does not allow this operation.",
                status_code=409,
            )

    @staticmethod
    async def _target(
        session: AsyncSession,
        *,
        space_id: UUID,
        action: str,
        node_id: UUID | None,
        revision_id: UUID | None,
        for_update: bool,
    ) -> tuple[KnowledgeNode, KnowledgeRevision | None] | None:
        if action == KnowledgeUpdateAction.CREATE.value and node_id is None:
            return None
        if action == KnowledgeUpdateAction.CREATE.value and revision_id is not None:
            raise AppError(
                "invalid_proposal_payload",
                "Create proposals cannot target a revision.",
                status_code=422,
            )
        if node_id is None or (
            action != KnowledgeUpdateAction.CREATE.value and revision_id is None
        ):
            raise AppError(
                "proposal_target_not_found",
                "A valid target node and revision are required.",
                status_code=404,
            )
        node_statement = select(KnowledgeNode).where(
            KnowledgeNode.id == node_id,
            KnowledgeNode.space_id == space_id,
            KnowledgeNode.deleted_at.is_(None),
        )
        if for_update:
            node_statement = node_statement.with_for_update()
        node = await session.scalar(node_statement)
        if node is None:
            raise AppError(
                "proposal_target_not_found", "Target node was not found.", status_code=404
            )
        if revision_id is None:
            return node, None
        revision = await session.scalar(
            select(KnowledgeRevision).where(
                KnowledgeRevision.id == revision_id,
                KnowledgeRevision.node_id == node.id,
                KnowledgeRevision.space_id == space_id,
            )
        )
        if revision is None:
            raise AppError(
                "proposal_target_not_found", "Target revision was not found.", status_code=404
            )
        return node, revision

    async def _freeze_evidence(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        proposal: KnowledgeUpdateProposal,
        inputs: list[ProposalEvidenceInput],
    ) -> list[KnowledgeUpdateProposalEvidence]:
        unique = {(item.section_id, item.role.value) for item in inputs}
        if len(unique) != len(inputs):
            raise AppError(
                "proposal_evidence_invalid",
                "Proposal evidence contains duplicate section roles.",
                status_code=409,
            )
        rows: list[KnowledgeUpdateProposalEvidence] = []
        for ordinal, item in enumerate(inputs):
            source_row = await session.execute(
                select(Source, SourceVersion, SourceParseArtifact, SourceSection)
                .join(SourceVersion, SourceVersion.source_id == Source.id)
                .join(
                    SourceParseArtifact,
                    SourceParseArtifact.id == SourceVersion.current_parse_artifact_id,
                )
                .join(SourceSection, SourceSection.id == item.section_id)
                .where(
                    Source.id == SourceVersion.source_id,
                    Source.space_id == space_id,
                    SourceVersion.id == SourceSection.source_version_id,
                    SourceSection.space_id == space_id,
                    SourceParseArtifact.status == ParseArtifactStatus.READY.value,
                    SourceVersion.parse_status == "ready",
                )
            )
            source, version, artifact, section = source_row.one_or_none() or (
                None,
                None,
                None,
                None,
            )
            if (
                source is None
                or version is None
                or artifact is None
                or section is None
                or section.parse_artifact_id != artifact.id
            ):
                raise AppError(
                    "proposal_evidence_invalid",
                    "Evidence must belong to a current ready source section in this space.",
                    status_code=409,
                )
            assert source is not None
            assert version is not None
            assert artifact is not None
            assert section is not None
            _validate_section_identity(section, version, artifact)
            if item.role.value in REVISION_EVIDENCE_ROLES:
                if item.knowledge_revision_id is None:
                    raise AppError(
                        "proposal_evidence_invalid",
                        "This evidence role requires a knowledge revision.",
                        status_code=409,
                    )
                link = await session.scalar(
                    select(KnowledgeEvidence).where(
                        KnowledgeEvidence.revision_id == item.knowledge_revision_id,
                        KnowledgeEvidence.space_id == space_id,
                        KnowledgeEvidence.section_id == section.id,
                        KnowledgeEvidence.source_version_id == version.id,
                        KnowledgeEvidence.parse_artifact_id == artifact.id,
                        KnowledgeEvidence.quote_hash == section.quote_hash,
                        KnowledgeEvidence.content_hash == section.content_hash,
                    )
                )
                if link is None:
                    raise AppError(
                        "proposal_evidence_invalid",
                        "Revision evidence does not match the selected section.",
                        status_code=409,
                    )
            elif item.knowledge_revision_id is not None:
                revision = await session.scalar(
                    select(KnowledgeRevision).where(
                        KnowledgeRevision.id == item.knowledge_revision_id,
                        KnowledgeRevision.space_id == space_id,
                    )
                )
                if revision is None:
                    raise AppError(
                        "proposal_evidence_invalid",
                        "Evidence revision is outside this space.",
                        status_code=409,
                    )
            rows.append(
                KnowledgeUpdateProposalEvidence(
                    id=uuid4(),
                    proposal_id=proposal.id,
                    space_id=space_id,
                    role=item.role.value,
                    source_id=source.id,
                    source_version_id=version.id,
                    parse_artifact_id=artifact.id,
                    section_id=section.id,
                    knowledge_revision_id=item.knowledge_revision_id,
                    frozen_quote=section.text,
                    quote_hash=section.quote_hash,
                    content_hash=section.content_hash,
                    locator=dict(section.locator),
                    ordinal=ordinal,
                )
            )
        return rows

    async def _validate_frozen_evidence(
        self, session: AsyncSession, proposal: KnowledgeUpdateProposal
    ) -> None:
        evidence = await self.evidence(session, proposal_id=proposal.id)
        if not evidence:
            raise AppError(
                "proposal_evidence_missing",
                "At least one valid evidence section is required.",
                status_code=409,
            )
        for item in evidence:
            row = await session.execute(
                select(SourceVersion, SourceParseArtifact, SourceSection)
                .join(
                    SourceParseArtifact,
                    SourceParseArtifact.id == SourceVersion.current_parse_artifact_id,
                )
                .join(SourceSection, SourceSection.id == item.section_id)
                .where(
                    SourceVersion.id == item.source_version_id,
                    SourceSection.space_id == proposal.space_id,
                )
            )
            version, artifact, section = row.one_or_none() or (None, None, None)
            valid = (
                version is not None
                and artifact is not None
                and section is not None
                and version.source_id == item.source_id
                and version.current_parse_artifact_id == artifact.id
                and version.parse_status == ParseStatus.READY.value
                and artifact.source_version_id == version.id
                and artifact.status == ParseArtifactStatus.READY.value
                and section.source_version_id == version.id
                and section.parse_artifact_id == item.parse_artifact_id
                and section.quote_hash == item.quote_hash
                and section.content_hash == item.content_hash
                and section.text == item.frozen_quote
                and dict(section.locator) == dict(item.locator)
            )
            if not valid:
                raise AppError(
                    "proposal_evidence_stale",
                    "Proposal evidence no longer matches its source section.",
                    status_code=409,
                )
            assert version is not None
            assert artifact is not None
            assert section is not None
            _validate_section_identity(section, version, artifact)
            if item.role in REVISION_EVIDENCE_ROLES:
                link = await session.scalar(
                    select(KnowledgeEvidence).where(
                        KnowledgeEvidence.revision_id == item.knowledge_revision_id,
                        KnowledgeEvidence.space_id == proposal.space_id,
                        KnowledgeEvidence.source_version_id == version.id,
                        KnowledgeEvidence.parse_artifact_id == artifact.id,
                        KnowledgeEvidence.section_id == section.id,
                        KnowledgeEvidence.quote_hash == section.quote_hash,
                        KnowledgeEvidence.content_hash == section.content_hash,
                    )
                )
                if link is None:
                    raise AppError(
                        "proposal_evidence_stale",
                        "Referenced knowledge evidence is no longer valid.",
                        status_code=409,
                    )


def _proposal_snapshot(proposal: KnowledgeUpdateProposal) -> dict[str, Any]:
    return {
        "id": str(proposal.id),
        "action": proposal.action,
        "status": proposal.status,
        "version": proposal.version,
        "target_node_id": str(proposal.target_node_id) if proposal.target_node_id else None,
        "target_revision_id": str(proposal.target_revision_id)
        if proposal.target_revision_id
        else None,
        "target_node_version": proposal.target_node_version,
        "suggested_title": proposal.suggested_title,
        "suggested_body": proposal.suggested_body,
        "suggested_tags": list(proposal.suggested_tags),
        "conditions": list(proposal.conditions),
        "exceptions": list(proposal.exceptions),
        "comparison_summary": proposal.comparison_summary,
        "confidence": proposal.confidence,
        "uncertainty_reason": proposal.uncertainty_reason,
        "superseded_by_proposal_id": str(proposal.superseded_by_proposal_id)
        if proposal.superseded_by_proposal_id
        else None,
    }


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise AppError(
            "invalid_proposal_payload", "Text fields must not be blank.", status_code=422
        )
    return cleaned


def _clean_list(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def _validate_section_identity(
    section: SourceSection, version: SourceVersion, artifact: SourceParseArtifact
) -> None:
    locator = section.locator
    valid = (
        section.source_version_id == version.id
        and section.parse_artifact_id == artifact.id
        and locator.get("sourceVersionId") == str(version.id)
        and locator.get("parseArtifactId") == str(artifact.id)
        and locator.get("sectionId") == str(section.id)
        and locator.get("quoteHash") == section.quote_hash
    )
    if not valid:
        raise AppError(
            "proposal_evidence_stale", "Source section identity validation failed.", status_code=409
        )
