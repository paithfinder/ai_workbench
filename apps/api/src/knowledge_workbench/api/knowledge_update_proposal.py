from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, status
from pydantic import BaseModel

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.application.knowledge_update_proposal import (
    KnowledgeUpdateProposalService,
    ProposalCreate,
    ProposalDecision,
    ProposalEdit,
    ProposalOutcome,
    ProposalSubmit,
    ProposalSupersede,
)
from knowledge_workbench.application.proposal_apply import ProposalApplyService
from knowledge_workbench.core.errors import ErrorEnvelope
from knowledge_workbench.db.models import (
    KnowledgeUpdateProposal,
    KnowledgeUpdateProposalEvidence,
    KnowledgeUpdateProposalTransition,
)

router = APIRouter(
    prefix="/api/v1/knowledge-spaces/{space_id}", tags=["knowledge-update-proposals"]
)
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 422)
}


class ProposalResultResponse(BaseModel):
    result_id: UUID
    proposal_id: UUID
    proposal_version: int
    proposal_status: str
    snapshot: dict[str, Any]


class ProposalItemResponse(BaseModel):
    id: UUID
    space_id: UUID
    research_run_id: UUID | None
    target_node_id: UUID | None
    target_revision_id: UUID | None
    target_node_version: int | None
    action: str
    create_kind: str | None
    status: str
    version: int
    suggested_title: str | None
    suggested_body: str | None
    suggested_tags: list[str]
    conditions: list[str]
    exceptions: list[str]
    comparison_summary: str | None
    confidence: float | None
    uncertainty_reason: str | None
    superseded_by_proposal_id: UUID | None
    submitted_at: Any
    reviewed_at: Any
    applied_at: Any
    superseded_at: Any
    created_at: Any
    updated_at: Any

    @classmethod
    def from_model(cls, item: KnowledgeUpdateProposal) -> ProposalItemResponse:
        return cls.model_validate(item, from_attributes=True)


class ProposalEvidenceResponse(BaseModel):
    id: UUID
    role: str
    source_id: UUID
    source_version_id: UUID
    parse_artifact_id: UUID
    section_id: UUID
    knowledge_revision_id: UUID | None
    frozen_quote: str
    quote_hash: str
    content_hash: str
    locator: dict[str, Any]
    ordinal: int

    @classmethod
    def from_model(cls, item: KnowledgeUpdateProposalEvidence) -> ProposalEvidenceResponse:
        return cls.model_validate(item, from_attributes=True)


class ProposalTransitionResponse(BaseModel):
    id: UUID
    request_id: UUID
    operation: str
    actor: str
    reason: str | None
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any]
    from_status: str
    to_status: str
    from_version: int
    to_version: int
    created_at: Any

    @classmethod
    def from_model(cls, item: KnowledgeUpdateProposalTransition) -> ProposalTransitionResponse:
        return cls.model_validate(item, from_attributes=True)


class ProposalDetailResponse(BaseModel):
    proposal: ProposalItemResponse
    evidence: list[ProposalEvidenceResponse]
    transitions: list[ProposalTransitionResponse]


class ProposalListResponse(BaseModel):
    items: list[ProposalItemResponse]


class ProposalRequestResponse(BaseModel):
    idempotency_key: str
    action: str
    result: ProposalResultResponse


def _result(outcome: ProposalOutcome) -> ProposalResultResponse:
    return ProposalResultResponse(
        result_id=outcome.result_id,
        proposal_id=outcome.proposal_id,
        proposal_version=outcome.proposal_version,
        proposal_status=outcome.proposal_status,
        snapshot=outcome.snapshot,
    )


@router.post(
    "/proposals",
    response_model=ProposalResultResponse,
    status_code=status.HTTP_200_OK,
    responses=ERROR_RESPONSES,
    operation_id="create_knowledge_update_proposal",
)
async def create_proposal(
    space_id: UUID,
    request: ProposalCreate,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> ProposalResultResponse:
    async with session.begin():
        outcome = await KnowledgeUpdateProposalService().create(
            session, space_id=space_id, idempotency_key=idempotency_key, request=request
        )
    return _result(outcome)


@router.get(
    "/proposals",
    response_model=ProposalListResponse,
    responses={422: {"model": ErrorEnvelope}},
    operation_id="list_knowledge_update_proposals",
)
async def list_proposals(
    space_id: UUID,
    session: SessionDependency,
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    action: str | None = Query(default=None, max_length=32),
    target_node_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProposalListResponse:
    items = await KnowledgeUpdateProposalService().list_proposals(
        session,
        space_id=space_id,
        status=status_filter,
        action=action,
        target_node_id=target_node_id,
        limit=limit,
        offset=offset,
    )
    return ProposalListResponse(items=[ProposalItemResponse.from_model(item) for item in items])


@router.get(
    "/proposals/{proposal_id}",
    response_model=ProposalDetailResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="get_knowledge_update_proposal",
)
async def get_proposal(
    space_id: UUID, proposal_id: UUID, session: SessionDependency
) -> ProposalDetailResponse:
    proposal, evidence, transitions = await KnowledgeUpdateProposalService().get_record(
        session, space_id=space_id, proposal_id=proposal_id
    )
    return ProposalDetailResponse(
        proposal=ProposalItemResponse.from_model(proposal),
        evidence=[ProposalEvidenceResponse.from_model(item) for item in evidence],
        transitions=[ProposalTransitionResponse.from_model(item) for item in transitions],
    )


@router.get(
    "/proposal-requests/{idempotency_key}",
    response_model=ProposalRequestResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="get_knowledge_update_proposal_request",
)
async def get_proposal_request(
    space_id: UUID, idempotency_key: str, session: SessionDependency
) -> ProposalRequestResponse:
    request_row, outcome = await KnowledgeUpdateProposalService().get_request(
        session, space_id=space_id, idempotency_key=idempotency_key
    )
    return ProposalRequestResponse(
        idempotency_key=request_row.idempotency_key,
        action=request_row.operation,
        result=_result(outcome),
    )


@router.patch(
    "/proposals/{proposal_id}",
    response_model=ProposalResultResponse,
    responses=ERROR_RESPONSES,
    operation_id="edit_knowledge_update_proposal",
)
async def edit_proposal(
    space_id: UUID,
    proposal_id: UUID,
    request: ProposalEdit,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> ProposalResultResponse:
    async with session.begin():
        outcome = await KnowledgeUpdateProposalService().edit(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _result(outcome)


@router.post(
    "/proposals/{proposal_id}/submit",
    response_model=ProposalResultResponse,
    responses=ERROR_RESPONSES,
    operation_id="submit_knowledge_update_proposal",
)
async def submit_proposal(
    space_id: UUID,
    proposal_id: UUID,
    request: ProposalSubmit,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> ProposalResultResponse:
    async with session.begin():
        outcome = await KnowledgeUpdateProposalService().submit(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _result(outcome)


@router.post(
    "/proposals/{proposal_id}/approve",
    response_model=ProposalResultResponse,
    responses=ERROR_RESPONSES,
    operation_id="approve_knowledge_update_proposal",
)
async def approve_proposal(
    space_id: UUID,
    proposal_id: UUID,
    request: ProposalDecision,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> ProposalResultResponse:
    async with session.begin():
        outcome = await KnowledgeUpdateProposalService().approve(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _result(outcome)


@router.post(
    "/proposals/{proposal_id}/apply",
    response_model=ProposalResultResponse,
    responses=ERROR_RESPONSES,
    operation_id="apply_knowledge_update_proposal",
)
async def apply_proposal(
    space_id: UUID,
    proposal_id: UUID,
    request: ProposalDecision,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ProposalResultResponse:
    async with session.begin():
        outcome = await ProposalApplyService().apply(
            session,
            settings=settings,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _result(outcome)


@router.post(
    "/proposals/{proposal_id}/reject",
    response_model=ProposalResultResponse,
    responses=ERROR_RESPONSES,
    operation_id="reject_knowledge_update_proposal",
)
async def reject_proposal(
    space_id: UUID,
    proposal_id: UUID,
    request: ProposalDecision,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> ProposalResultResponse:
    async with session.begin():
        outcome = await KnowledgeUpdateProposalService().reject(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _result(outcome)


@router.post(
    "/proposals/{proposal_id}/supersede",
    response_model=ProposalResultResponse,
    responses=ERROR_RESPONSES,
    operation_id="supersede_knowledge_update_proposal",
)
async def supersede_proposal(
    space_id: UUID,
    proposal_id: UUID,
    request: ProposalSupersede,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> ProposalResultResponse:
    async with session.begin():
        outcome = await KnowledgeUpdateProposalService().supersede(
            session,
            space_id=space_id,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _result(outcome)
