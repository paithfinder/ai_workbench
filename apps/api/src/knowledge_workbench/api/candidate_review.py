from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import BaseModel, Field

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.application.candidate_review import (
    CandidateAccept,
    CandidateEdit,
    CandidateNeedsVerification,
    CandidateReject,
    CandidateReviewService,
    DestinationRecord,
    ReviewOutcome,
)
from knowledge_workbench.application.indexing import IndexingService
from knowledge_workbench.core.errors import ErrorEnvelope

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}", tags=["candidate-review"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 422)
}


class CandidateReviewResponse(BaseModel):
    result_id: UUID
    candidate_review_id: UUID
    candidate_id: UUID
    candidate_version: int
    candidate_status: str
    knowledge_node_id: UUID | None
    knowledge_revision_id: UUID | None
    review_card_id: UUID | None
    evidence_ids: list[UUID]


class CandidateReviewRequestResponse(BaseModel):
    idempotency_key: str
    action: str
    status: str = "succeeded"
    result: CandidateReviewResponse
    error_code: str | None = None
    error_message: str | None = None


class DestinationResponse(BaseModel):
    id: UUID
    name: str
    path: list[str] = Field(default_factory=list)


class DestinationListResponse(BaseModel):
    items: list[DestinationResponse]


def _review_response(outcome: ReviewOutcome) -> CandidateReviewResponse:
    return CandidateReviewResponse(
        result_id=outcome.result_id,
        candidate_review_id=outcome.candidate_review_id,
        candidate_id=outcome.candidate_id,
        candidate_version=outcome.candidate_version,
        candidate_status=outcome.candidate_status,
        knowledge_node_id=outcome.knowledge_node_id,
        knowledge_revision_id=outcome.knowledge_revision_id,
        review_card_id=outcome.review_card_id,
        evidence_ids=outcome.evidence_ids,
    )


def _destination_response(record: DestinationRecord) -> DestinationResponse:
    return DestinationResponse(
        id=record.node.id,
        name=record.revision.title,
        path=[record.revision.title],
    )


@router.get(
    "/knowledge-destinations",
    response_model=DestinationListResponse,
    responses={422: {"model": ErrorEnvelope}},
    operation_id="list_knowledge_destinations",
)
async def list_destinations(
    space_id: UUID, session: SessionDependency
) -> DestinationListResponse:
    records = await CandidateReviewService().list_destinations(session, space_id=space_id)
    return DestinationListResponse(items=[_destination_response(record) for record in records])


@router.get(
    "/candidates/{candidate_id}/review-requests/{idempotency_key}",
    response_model=CandidateReviewRequestResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="get_candidate_review_request",
)
async def get_candidate_review_request(
    space_id: UUID,
    candidate_id: UUID,
    idempotency_key: str,
    session: SessionDependency,
) -> CandidateReviewRequestResponse:
    request_row, outcome = await CandidateReviewService().get_request(
        session,
        space_id=space_id,
        candidate_id=candidate_id,
        idempotency_key=idempotency_key,
    )
    return CandidateReviewRequestResponse(
        idempotency_key=request_row.idempotency_key,
        action=request_row.operation,
        result=_review_response(outcome),
    )


@router.patch(
    "/candidates/{candidate_id}",
    response_model=CandidateReviewResponse,
    responses=ERROR_RESPONSES,
    operation_id="edit_extraction_candidate",
)
async def edit_candidate(
    space_id: UUID,
    candidate_id: UUID,
    request: CandidateEdit,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> CandidateReviewResponse:
    async with session.begin():
        outcome = await CandidateReviewService().edit(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _review_response(outcome)


@router.post(
    "/candidates/{candidate_id}/accept",
    response_model=CandidateReviewResponse,
    status_code=status.HTTP_200_OK,
    responses=ERROR_RESPONSES,
    operation_id="accept_extraction_candidate",
)
async def accept_candidate(
    space_id: UUID,
    candidate_id: UUID,
    request: CandidateAccept,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> CandidateReviewResponse:
    async with session.begin():
        outcome = await CandidateReviewService().accept(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            request=request,
        )
        if outcome.knowledge_revision_id is not None:
            await IndexingService().request_knowledge_rebuild(
                session,
                settings=settings,
                space_id=space_id,
                revision_id=outcome.knowledge_revision_id,
                idempotency_key=f"candidate-accept:{outcome.result_id}",
            )
    return _review_response(outcome)


@router.post(
    "/candidates/{candidate_id}/mark-needs-verification",
    response_model=CandidateReviewResponse,
    responses=ERROR_RESPONSES,
    operation_id="mark_candidate_needs_verification",
)
async def mark_candidate_needs_verification(
    space_id: UUID,
    candidate_id: UUID,
    request: CandidateNeedsVerification,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> CandidateReviewResponse:
    async with session.begin():
        outcome = await CandidateReviewService().mark_needs_verification(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _review_response(outcome)


@router.post(
    "/candidates/{candidate_id}/reject",
    response_model=CandidateReviewResponse,
    responses=ERROR_RESPONSES,
    operation_id="reject_extraction_candidate",
)
async def reject_candidate(
    space_id: UUID,
    candidate_id: UUID,
    request: CandidateReject,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> CandidateReviewResponse:
    async with session.begin():
        outcome = await CandidateReviewService().reject(
            session,
            space_id=space_id,
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _review_response(outcome)
