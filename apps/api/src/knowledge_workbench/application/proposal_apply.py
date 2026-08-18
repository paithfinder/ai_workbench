from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.indexing import IndexingService
from knowledge_workbench.application.knowledge_tree import KnowledgeTreeService
from knowledge_workbench.application.knowledge_update_proposal import (
    KnowledgeUpdateProposalService,
    ProposalDecision,
    ProposalOutcome,
    _proposal_snapshot,
    proposal_request_hash,
)
from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeUpdateAction,
    KnowledgeUpdateProposal,
    KnowledgeUpdateProposalEvidence,
    KnowledgeUpdateProposalOperation,
    KnowledgeUpdateProposalStatus,
)


class ProposalApplyService:
    async def apply(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        space_id: UUID,
        proposal_id: UUID,
        idempotency_key: str,
        request: ProposalDecision,
    ) -> ProposalOutcome:
        key = _validate_idempotency_key(idempotency_key)
        operation = KnowledgeUpdateProposalOperation.APPLY
        request_hash = proposal_request_hash(operation, request, proposal_id=proposal_id)
        await KnowledgeUpdateProposalService._lock_idempotency_key(
            session, space_id=space_id, key=key
        )
        replay = await KnowledgeUpdateProposalService()._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=request_hash,
            proposal_id=proposal_id,
        )
        if replay is not None:
            return replay
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(space_id)))))
        proposal = await KnowledgeUpdateProposalService._lock_proposal(
            session, space_id, proposal_id, for_update=True
        )
        replay = await KnowledgeUpdateProposalService()._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=request_hash,
            proposal_id=proposal_id,
        )
        if replay is not None:
            return replay
        KnowledgeUpdateProposalService._ensure_status(
            proposal,
            {KnowledgeUpdateProposalStatus.APPROVED.value},
            request.expected_version,
        )

        target = await KnowledgeUpdateProposalService._target(
            session,
            space_id=space_id,
            action=proposal.action,
            node_id=proposal.target_node_id,
            revision_id=proposal.target_revision_id,
            for_update=True,
        )
        self._validate_target_snapshot(proposal, target)
        await KnowledgeUpdateProposalService()._validate_frozen_evidence(session, proposal)
        evidence = list(
            await session.scalars(
                select(KnowledgeUpdateProposalEvidence)
                .where(
                    KnowledgeUpdateProposalEvidence.proposal_id == proposal.id,
                    KnowledgeUpdateProposalEvidence.space_id == space_id,
                )
                .order_by(KnowledgeUpdateProposalEvidence.ordinal)
            )
        )

        action = KnowledgeUpdateAction(proposal.action)
        revision_id: UUID | None = None
        old_revision_id = proposal.target_revision_id
        node_id: UUID | None = proposal.target_node_id
        node_version: int | None = None
        evidence_ids: list[UUID] = []
        index_job_id: UUID | None = None
        index_run_id: UUID | None = None
        if action is KnowledgeUpdateAction.MARK_REVIEW_RECOMMENDED:
            pass
        elif target is None:
            raise AppError(
                "proposal_target_not_found",
                "An apply target is required for this proposal.",
                status_code=409,
            )
        elif action is KnowledgeUpdateAction.CREATE:
            parent, _ = target
            if proposal.create_kind not in {
                KnowledgeNodeKind.FOLDER.value,
                KnowledgeNodeKind.DOCUMENT.value,
            }:
                raise AppError(
                    "proposal_create_kind_invalid",
                    "Create proposal is missing an explicit folder or document kind.",
                    status_code=409,
                )
            node, revision, created_evidence, _create_card = (
                await KnowledgeTreeService().create_proposal_node(
                    session,
                    space_id=space_id,
                    parent=parent,
                    kind=KnowledgeNodeKind(proposal.create_kind),
                    title=self._required_title(proposal),
                    body=proposal.suggested_body or "",
                    tags=list(proposal.suggested_tags),
                    conditions=list(proposal.conditions),
                    exceptions=list(proposal.exceptions),
                    evidence=evidence,
                    reason=request.reason,
                )
            )
            node_id = node.id
            node_version = node.version
            revision_id = revision.id
            evidence_ids = [item.id for item in created_evidence]
        elif action in {
            KnowledgeUpdateAction.REVISE,
            KnowledgeUpdateAction.MERGE_SUGGESTION,
        }:
            node, current = target
            if current is None:
                raise AppError(
                    "proposal_target_stale",
                    "Proposal target has no current revision.",
                    status_code=409,
                )
            revision, created_evidence, _revision_card = (
                await KnowledgeTreeService().append_proposal_revision(
                    session,
                    node=node,
                    current=current,
                    title=proposal.suggested_title or current.title,
                    body=proposal.suggested_body or current.body,
                    tags=list(proposal.suggested_tags),
                    conditions=list(proposal.conditions),
                    exceptions=list(proposal.exceptions),
                    evidence=evidence,
                    reason=request.reason,
                )
            )
            node_id = node.id
            node_version = node.version
            revision_id = revision.id
            evidence_ids = [item.id for item in created_evidence]
        else:
            raise AppError(
                "proposal_action_unsupported",
                "This proposal action cannot be applied to formal knowledge.",
                status_code=409,
            )

        if revision_id is not None:
            index = await IndexingService().request_knowledge_rebuild(
                session,
                settings=settings,
                space_id=space_id,
                revision_id=revision_id,
                idempotency_key=f"proposal-apply:{proposal.id}:{revision_id}",
            )
            index_job_id = index.job.id
            index_run_id = index.run.id
        before = _proposal_snapshot(proposal)
        proposal.applied_at = datetime.now(UTC)
        return await KnowledgeUpdateProposalService()._record(
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
            to_status=KnowledgeUpdateProposalStatus.APPLIED.value,
            result_snapshot={
                "apply": {
                    "action": proposal.action,
                    "target_node_id": str(node_id) if node_id else None,
                    "old_revision_id": str(old_revision_id) if old_revision_id else None,
                    "new_revision_id": str(revision_id) if revision_id else None,
                    "node_version": node_version,
                    "evidence_ids": [str(item) for item in evidence_ids],
                    "index_job_id": str(index_job_id) if index_job_id else None,
                    "index_run_id": str(index_run_id) if index_run_id else None,
                }
            },
        )

    @staticmethod
    def _required_title(proposal: KnowledgeUpdateProposal) -> str:
        if proposal.suggested_title is None:
            raise AppError(
                "proposal_apply_content_invalid",
                "Proposal needs a suggested title before it can create knowledge.",
                status_code=409,
            )
        return proposal.suggested_title

    @staticmethod
    def _validate_target_snapshot(
        proposal: KnowledgeUpdateProposal,
        target: tuple[KnowledgeNode, Any] | None,
    ) -> None:
        if target is None:
            raise AppError(
                "proposal_target_stale",
                "Proposal target is no longer available.",
                status_code=409,
            )
        node, revision = target
        if proposal.target_node_version is None or node.version != proposal.target_node_version:
            raise AppError(
                "proposal_target_stale",
                "Proposal target knowledge version is stale.",
                status_code=409,
            )
        if proposal.action != KnowledgeUpdateAction.CREATE.value:
            if revision is None or proposal.target_revision_id != revision.id:
                raise AppError(
                    "proposal_target_stale",
                    "Proposal target revision is no longer current.",
                    status_code=409,
                )
            if node.current_revision_id != revision.id:
                raise AppError(
                    "proposal_target_stale",
                    "Proposal target revision is no longer current.",
                    status_code=409,
                )

