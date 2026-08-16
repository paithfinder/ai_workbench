from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import BaseModel

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.application.proposal_comparison import (
    ProposalComparisonOutcome,
    ProposalComparisonRequest,
    ProposalComparisonService,
)
from knowledge_workbench.core.errors import ErrorEnvelope
from knowledge_workbench.db.models import ProposalComparisonCandidate

router = APIRouter(
    prefix="/api/v1/knowledge-spaces/{space_id}",
    tags=["proposal-comparisons"],
)
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 422, 502)
}


class ProposalComparisonCandidateResponse(BaseModel):
    candidate_id: str
    ordinal: int
    candidate_kind: str
    selection_id: UUID | None
    source_id: UUID
    source_version_id: UUID
    parse_artifact_id: UUID
    section_id: UUID
    knowledge_node_id: UUID | None
    knowledge_revision_id: UUID | None
    knowledge_evidence_id: UUID | None
    title: str | None
    quote_hash: str
    content_hash: str
    locator: dict[str, Any]
    keyword_rank: int | None
    keyword_score: float | None
    vector_rank: int | None
    vector_score: float | None
    rrf_rank: int | None
    rrf_score: float | None
    rerank_rank: int | None
    rerank_score: float | None
    included_in_context: bool
    context_ordinal: int | None

    @classmethod
    def from_model(cls, item: ProposalComparisonCandidate) -> ProposalComparisonCandidateResponse:
        return cls.model_validate(item, from_attributes=True)


class ProposalComparisonResponse(BaseModel):
    id: UUID
    research_run_id: UUID
    selection_batch_id: UUID
    scope_node_id: UUID
    include_descendants: bool
    scope_snapshot: dict[str, Any]
    scope_snapshot_hash: str
    index_config_version: str | None
    retrieval_config: dict[str, Any]
    reranker_config: dict[str, Any]
    context_config: dict[str, Any]
    ai_provider: str
    ai_model: str
    prompt_version: str
    schema_version: str
    status: str
    comparison_kind: str | None
    proposal_id: UUID | None
    failure_code: str | None
    input_tokens: int
    output_tokens: int
    timings_ms: dict[str, Any]
    warnings: list[str]
    provider_request_id: str | None
    started_at: Any
    completed_at: Any
    created_at: Any
    candidates: list[ProposalComparisonCandidateResponse]

    @classmethod
    def from_outcome(cls, outcome: ProposalComparisonOutcome) -> ProposalComparisonResponse:
        run = outcome.run
        return cls(
            id=run.id,
            research_run_id=run.research_run_id,
            selection_batch_id=run.selection_batch_id,
            scope_node_id=run.scope_node_id,
            include_descendants=run.include_descendants,
            scope_snapshot=run.scope_snapshot,
            scope_snapshot_hash=run.scope_snapshot_hash,
            index_config_version=run.index_config_version,
            retrieval_config=run.retrieval_config,
            reranker_config=run.reranker_config,
            context_config=run.context_config,
            ai_provider=run.ai_provider,
            ai_model=run.ai_model,
            prompt_version=run.prompt_version,
            schema_version=run.schema_version,
            status=run.status,
            comparison_kind=run.comparison_kind,
            proposal_id=run.proposal_id,
            failure_code=run.failure_code,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            timings_ms=run.timings_ms,
            warnings=run.warnings,
            provider_request_id=run.provider_request_id,
            started_at=run.started_at,
            completed_at=run.completed_at,
            created_at=run.created_at,
            candidates=[
                ProposalComparisonCandidateResponse.from_model(item) for item in outcome.candidates
            ],
        )


@router.post(
    "/research-runs/{research_run_id}/proposal-comparisons",
    response_model=ProposalComparisonResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
    operation_id="create_proposal_comparison",
)
async def create_proposal_comparison(
    space_id: UUID,
    research_run_id: UUID,
    body: ProposalComparisonRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ProposalComparisonResponse:
    outcome = await ProposalComparisonService().compare(
        session,
        settings=settings,
        space_id=space_id,
        research_run_id=research_run_id,
        idempotency_key=idempotency_key,
        request=body,
    )
    return ProposalComparisonResponse.from_outcome(outcome)


@router.get(
    "/proposal-comparisons/{comparison_id}",
    response_model=ProposalComparisonResponse,
    responses={404: {"model": ErrorEnvelope}},
    operation_id="get_proposal_comparison",
)
async def get_proposal_comparison(
    space_id: UUID,
    comparison_id: UUID,
    session: SessionDependency,
) -> ProposalComparisonResponse:
    outcome = await ProposalComparisonService().get(
        session,
        space_id=space_id,
        comparison_id=comparison_id,
    )
    return ProposalComparisonResponse.from_outcome(outcome)
