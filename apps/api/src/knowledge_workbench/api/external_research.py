from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import BaseModel

from knowledge_workbench.api.dependencies import (
    SessionDependency,
    SettingsDependency,
    StorageDependency,
)
from knowledge_workbench.application.external_research import (
    ExternalResearchService,
    SearchResearchRequest,
    SearchRunOutcome,
    SourceSelectionBatchOutcome,
    SourceSelectionRequest,
)
from knowledge_workbench.core.errors import ErrorEnvelope
from knowledge_workbench.db.models import (
    ResearchSearchResult,
    ResearchSourceSelection,
)
from knowledge_workbench.infrastructure.search.factory import create_search_provider

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}", tags=["external-research"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 413, 422, 503)
}


class SearchResultResponse(BaseModel):
    id: UUID
    ordinal: int
    title: str
    url: str
    snippet: str | None
    provider_metadata: dict[str, str]

    @classmethod
    def from_model(cls, item: ResearchSearchResult) -> SearchResultResponse:
        return cls.model_validate(item, from_attributes=True)


class ResearchRunResponse(BaseModel):
    id: UUID
    status: str
    origin: str
    query: str | None
    provider: str | None
    provider_model: str | None
    provider_request_id: str | None
    search_filters: dict[str, str]
    failure_code: str | None
    failure_message: str | None
    started_at: Any
    completed_at: Any
    created_at: Any
    results: list[SearchResultResponse]

    @classmethod
    def from_outcome(cls, outcome: SearchRunOutcome) -> ResearchRunResponse:
        run = outcome.research_run
        return cls(
            id=run.id,
            status=run.status,
            origin=run.origin,
            query=run.query,
            provider=run.provider,
            provider_model=run.provider_model,
            provider_request_id=run.provider_request_id,
            search_filters=run.search_filters,
            failure_code=run.failure_code,
            failure_message=run.failure_message,
            started_at=run.started_at,
            completed_at=run.completed_at,
            created_at=run.created_at,
            results=[SearchResultResponse.from_model(item) for item in outcome.results],
        )


class SourceSelectionResponse(BaseModel):
    id: UUID
    ordinal: int
    requested_url: str
    title: str
    status: str
    source_id: UUID | None
    source_version_id: UUID | None
    job_id: UUID | None
    error_code: str | None
    error_message: str | None
    completed_at: Any

    @classmethod
    def from_model(cls, item: ResearchSourceSelection) -> SourceSelectionResponse:
        return cls.model_validate(item, from_attributes=True)


class SourceSelectionBatchResponse(BaseModel):
    id: UUID
    research_run_id: UUID
    status: str
    created_at: Any
    completed_at: Any
    selections: list[SourceSelectionResponse]

    @classmethod
    def from_outcome(cls, outcome: SourceSelectionBatchOutcome) -> SourceSelectionBatchResponse:
        batch = outcome.batch
        return cls(
            id=batch.id,
            research_run_id=batch.research_run_id,
            status=batch.status,
            created_at=batch.created_at,
            completed_at=batch.completed_at,
            selections=[SourceSelectionResponse.from_model(item) for item in outcome.selections],
        )


@router.post(
    "/research-runs/search",
    response_model=ResearchRunResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
    operation_id="search_external_research",
)
async def search_research(
    space_id: UUID,
    body: SearchResearchRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ResearchRunResponse:
    outcome = await ExternalResearchService(create_search_provider(settings)).search(
        session,
        space_id=space_id,
        idempotency_key=idempotency_key,
        request=body,
        max_results=settings.search_max_results,
    )
    return ResearchRunResponse.from_outcome(outcome)


@router.get(
    "/research-runs/{research_run_id}",
    response_model=ResearchRunResponse,
    responses={404: {"model": ErrorEnvelope}},
    operation_id="get_external_research_run",
)
async def get_research_run(
    space_id: UUID, research_run_id: UUID, session: SessionDependency
) -> ResearchRunResponse:
    outcome = await ExternalResearchService().get_run(
        session, space_id=space_id, research_run_id=research_run_id
    )
    return ResearchRunResponse.from_outcome(outcome)


@router.post(
    "/research-runs/{research_run_id}/source-selections",
    response_model=SourceSelectionBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    operation_id="select_external_research_sources",
)
async def select_research_sources(
    space_id: UUID,
    research_run_id: UUID,
    body: SourceSelectionRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    storage: StorageDependency,
    settings: SettingsDependency,
) -> SourceSelectionBatchResponse:
    outcome = await ExternalResearchService().select_sources(
        session,
        storage=storage,
        settings=settings,
        space_id=space_id,
        research_run_id=research_run_id,
        idempotency_key=idempotency_key,
        request=body,
    )
    return SourceSelectionBatchResponse.from_outcome(outcome)


@router.get(
    "/research-runs/{research_run_id}/source-selection-batches/{batch_id}",
    response_model=SourceSelectionBatchResponse,
    responses={404: {"model": ErrorEnvelope}},
    operation_id="get_external_research_source_selection_batch",
)
async def get_source_selection_batch(
    space_id: UUID,
    research_run_id: UUID,
    batch_id: UUID,
    session: SessionDependency,
) -> SourceSelectionBatchResponse:
    outcome = await ExternalResearchService().get_batch(
        session,
        space_id=space_id,
        research_run_id=research_run_id,
        batch_id=batch_id,
    )
    return SourceSelectionBatchResponse.from_outcome(outcome)
