from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.non_file_ingestion import NonFileIngestionRecord
from knowledge_workbench.application.ports.object_storage import ObjectStorage
from knowledge_workbench.application.ports.search_provider import (
    SearchProvider,
    SearchRequest,
    SearchResponse,
)
from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.application.web_source_ingestion import ingest_web_source
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    AcquisitionType,
    ActivityEvent,
    ActorType,
    Job,
    JobKind,
    KnowledgeSpace,
    ResearchRun,
    ResearchRunStatus,
    ResearchSearchRequest,
    ResearchSearchResult,
    ResearchSourceSelection,
    ResearchSourceSelectionBatch,
    ResearchSourceSelectionBatchStatus,
    ResearchSourceSelectionStatus,
    Source,
    SourceKind,
    SourceVersion,
)
from knowledge_workbench.infrastructure.web.safe_http import normalize_web_url


class SearchResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    max_results: int | None = Field(default=None, ge=1, le=50)
    filters: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        return normalized

    @field_validator("filters")
    @classmethod
    def normalize_filters(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, item in value.items():
            clean_key = key.strip()
            clean_value = item.strip()
            if not clean_key or not clean_value or len(clean_key) > 100 or len(clean_value) > 500:
                raise ValueError("filters must contain non-empty bounded strings")
            normalized[clean_key] = clean_value
        return normalized


class SourceSelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=500)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be empty")
        return normalized


class SourceSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[SourceSelectionInput] = Field(min_length=1, max_length=50)


@dataclass(frozen=True, slots=True)
class SearchRunOutcome:
    research_run: ResearchRun
    results: tuple[ResearchSearchResult, ...]


@dataclass(frozen=True, slots=True)
class SourceSelectionBatchOutcome:
    batch: ResearchSourceSelectionBatch
    selections: tuple[ResearchSourceSelection, ...]


class ExternalResearchService:
    def __init__(self, provider: SearchProvider | None = None) -> None:
        self._provider = provider

    async def search(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        idempotency_key: str,
        request: SearchResearchRequest,
        max_results: int,
    ) -> SearchRunOutcome:
        key = _validate_idempotency_key(idempotency_key)
        effective_limit = request.max_results or max_results
        if effective_limit > max_results:
            raise AppError(
                "search_result_limit_exceeded",
                "Search result count exceeds the configured limit.",
                status_code=422,
            )
        request_hash = _hash(
            {
                "operation": "external_search.v1",
                "query": request.query,
                "max_results": effective_limit,
                "filters": request.filters,
            }
        )
        async with session.begin():
            await self._space(session, space_id=space_id, for_update=True)
            existing = await session.scalar(
                select(ResearchSearchRequest)
                .where(
                    ResearchSearchRequest.space_id == space_id,
                    ResearchSearchRequest.idempotency_key == key,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise AppError(
                        "idempotency_conflict",
                        "This Idempotency-Key was already used with a different search request.",
                        status_code=409,
                    )
                existing_run = await self._run(
                    session,
                    space_id=space_id,
                    research_run_id=existing.research_run_id,
                )
                if existing_run.status == ResearchRunStatus.DRAFT.value:
                    raise AppError(
                        "external_search_in_progress",
                        "This external search request is already being processed.",
                        status_code=409,
                    )
                return await self._search_outcome(
                    session, space_id=space_id, research_run_id=existing.research_run_id
                )
            research_run = ResearchRun(
                id=uuid4(),
                space_id=space_id,
                origin="external_search",
                status=ResearchRunStatus.DRAFT.value,
                query=request.query,
                provider=None,
                provider_model=None,
                search_filters=dict(request.filters),
                started_at=datetime.now(UTC),
            )
            session.add_all(
                [
                    research_run,
                    ResearchSearchRequest(
                        id=uuid4(),
                        space_id=space_id,
                        research_run_id=research_run.id,
                        idempotency_key=key,
                        request_hash=request_hash,
                    ),
                ]
            )
            await session.flush()

        if self._provider is None:
            error = AppError(
                "external_search_not_configured",
                "External search is not configured for this environment.",
                status_code=503,
            )
            await self._mark_search_failed(
                session,
                space_id=space_id,
                research_run_id=research_run.id,
                error=error,
            )
            raise error
        try:
            response = await self._provider.search(
                SearchRequest(
                    query=request.query,
                    max_results=effective_limit,
                    filters=dict(request.filters),
                )
            )
            normalized = _normalize_results(response)
        except AppError as exc:
            await self._mark_search_failed(
                session,
                space_id=space_id,
                research_run_id=research_run.id,
                error=exc,
            )
            raise
        except Exception as exc:
            error = AppError(
                "external_search_unavailable",
                "External search is temporarily unavailable. Please retry.",
                status_code=503,
            )
            await self._mark_search_failed(
                session,
                space_id=space_id,
                research_run_id=research_run.id,
                error=error,
            )
            raise error from exc

        async with session.begin():
            current = await self._run(session, space_id=space_id, research_run_id=research_run.id)
            current.provider = response.provider
            current.provider_model = response.model
            current.provider_request_id = response.request_id
            current.status = ResearchRunStatus.COMPLETED.value
            current.completed_at = datetime.now(UTC)
            entries = [
                ResearchSearchResult(
                    id=uuid4(),
                    space_id=space_id,
                    research_run_id=current.id,
                    ordinal=index,
                    title=item.title,
                    url=item.url,
                    snippet=item.snippet,
                    provider_metadata=item.metadata or {},
                )
                for index, item in enumerate(normalized)
            ]
            session.add_all(
                [
                    *entries,
                    ActivityEvent(
                        id=uuid4(),
                        space_id=space_id,
                        event_type="research.search.completed",
                        entity_type="research_run",
                        entity_id=current.id,
                        actor_type=ActorType.USER.value,
                        payload={
                            "research_run_id": str(current.id),
                            "provider": response.provider,
                            "result_count": len(entries),
                        },
                    ),
                ]
            )
            await session.flush()
            return SearchRunOutcome(current, tuple(entries))

    async def get_run(
        self, session: AsyncSession, *, space_id: UUID, research_run_id: UUID
    ) -> SearchRunOutcome:
        research_run = await self._run(
            session, space_id=space_id, research_run_id=research_run_id
        )
        return await self._search_outcome(
            session, space_id=space_id, research_run_id=research_run.id
        )

    async def select_sources(
        self,
        session: AsyncSession,
        *,
        storage: ObjectStorage,
        settings: Settings,
        space_id: UUID,
        research_run_id: UUID,
        idempotency_key: str,
        request: SourceSelectionRequest,
    ) -> SourceSelectionBatchOutcome:
        key = _validate_idempotency_key(idempotency_key)
        selections = _normalize_selections(
            request.selections, max_urls=settings.research_selection_max_urls
        )
        request_hash = _hash(
            {
                "operation": "external_search.select_sources.v1",
                "selections": [
                    {"title": item.title, "url": item.url} for item in selections
                ],
            }
        )
        async with session.begin():
            run = await self._run(
                session,
                space_id=space_id,
                research_run_id=research_run_id,
                for_update=True,
            )
            if run.origin != "external_search" or run.status != ResearchRunStatus.COMPLETED.value:
                raise AppError(
                    "research_run_not_selectable",
                    "This research run is not ready for source selection.",
                    status_code=409,
                )
            existing = await session.scalar(
                select(ResearchSourceSelectionBatch)
                .where(
                    ResearchSourceSelectionBatch.research_run_id == research_run_id,
                    ResearchSourceSelectionBatch.idempotency_key == key,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise AppError(
                        "idempotency_conflict",
                        "This Idempotency-Key was already used with different source selections.",
                        status_code=409,
                    )
                if existing.status == ResearchSourceSelectionBatchStatus.PENDING.value:
                    raise AppError(
                        "source_selection_in_progress",
                        "This source selection request is already being processed.",
                        status_code=409,
                    )
                return await self._batch_outcome(
                    session,
                    space_id=space_id,
                    research_run_id=research_run_id,
                    batch_id=existing.id,
                )
            batch = ResearchSourceSelectionBatch(
                id=uuid4(),
                space_id=space_id,
                research_run_id=research_run_id,
                idempotency_key=key,
                request_hash=request_hash,
                status=ResearchSourceSelectionBatchStatus.PENDING.value,
            )
            entries = [
                ResearchSourceSelection(
                    id=uuid4(),
                    space_id=space_id,
                    research_run_id=research_run_id,
                    batch_id=batch.id,
                    ordinal=index,
                    requested_url=item.url,
                    title=item.title,
                    status=ResearchSourceSelectionStatus.PENDING.value,
                )
                for index, item in enumerate(selections)
            ]
            session.add_all([batch, *entries])
            await session.flush()

        for entry in entries:
            try:
                record = await self._existing_web_record(
                    session,
                    space_id=space_id,
                    requested_url=entry.requested_url,
                )
                if record is None:
                    record = await ingest_web_source(
                        session=session,
                        storage=storage,
                        settings=settings,
                        space_id=space_id,
                        title=entry.title,
                        url=entry.requested_url,
                        idempotency_key=f"research-selection:{batch.id}:{entry.ordinal}",
                    )
            except AppError as exc:
                await self._mark_selection_failed(
                    session,
                    selection_id=entry.id,
                    error=exc,
                )
            except Exception:
                await self._mark_selection_failed(
                    session,
                    selection_id=entry.id,
                    error=AppError(
                        "source_selection_failed",
                        "The selected web source could not be persisted.",
                        status_code=503,
                    ),
                )
            else:
                await self._mark_selection_succeeded(
                    session, selection_id=entry.id, record=record
                )

        async with session.begin():
            outcome = await self._batch_outcome(
                session, space_id=space_id, research_run_id=research_run_id, batch_id=batch.id
            )
            failures = sum(
                item.status == ResearchSourceSelectionStatus.FAILED.value
                for item in outcome.selections
            )
            outcome.batch.status = (
                ResearchSourceSelectionBatchStatus.COMPLETED_WITH_FAILURES.value
                if failures
                else ResearchSourceSelectionBatchStatus.COMPLETED.value
            )
            outcome.batch.completed_at = datetime.now(UTC)
            session.add(
                ActivityEvent(
                    id=uuid4(),
                    space_id=space_id,
                    event_type="research.source_selection.completed",
                    entity_type="research_source_selection_batch",
                    entity_id=outcome.batch.id,
                    actor_type=ActorType.USER.value,
                    payload={
                        "research_run_id": str(research_run_id),
                        "batch_id": str(outcome.batch.id),
                        "selected_count": len(outcome.selections),
                        "failure_count": failures,
                    },
                )
            )
            await session.flush()
            return outcome

    async def get_batch(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        research_run_id: UUID,
        batch_id: UUID,
    ) -> SourceSelectionBatchOutcome:
        return await self._batch_outcome(
            session,
            space_id=space_id,
            research_run_id=research_run_id,
            batch_id=batch_id,
        )

    async def _existing_web_record(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        requested_url: str,
    ) -> NonFileIngestionRecord | None:
        async with session.begin():
            row = (
                await session.execute(
                    select(Source.id, SourceVersion.id, Job.id, SourceVersion.storage_key)
                    .join(SourceVersion, SourceVersion.source_id == Source.id)
                    .join(
                        Job,
                        (Job.source_version_id == SourceVersion.id)
                        & (Job.space_id == Source.space_id)
                        & (Job.kind == JobKind.SOURCE_INGEST.value),
                    )
                    .where(
                        Source.space_id == space_id,
                        Source.kind == SourceKind.WEB.value,
                        Source.status != "deleted",
                        SourceVersion.acquisition_type == AcquisitionType.WEB_FETCH.value,
                        SourceVersion.acquisition_metadata["requested_url"].as_string()
                        == requested_url,
                        SourceVersion.storage_key.is_not(None),
                    )
                    .order_by(SourceVersion.completed_at.desc(), SourceVersion.id.desc())
                    .limit(1)
                )
            ).one_or_none()
        if row is None:
            return None
        source_id, version_id, job_id, storage_key = row
        return NonFileIngestionRecord(
            source_id=source_id,
            version_id=version_id,
            job_id=job_id,
            storage_key=storage_key,
            request_hash=_hash({"operation": "existing_web_source", "url": requested_url}),
        )

    async def _mark_search_failed(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        research_run_id: UUID,
        error: AppError,
    ) -> None:
        async with session.begin():
            run = await self._run(session, space_id=space_id, research_run_id=research_run_id)
            run.status = ResearchRunStatus.FAILED.value
            run.failure_code = error.code[:100]
            run.failure_message = error.message[:1000]
            run.completed_at = datetime.now(UTC)

    async def _mark_selection_succeeded(
        self, session: AsyncSession, *, selection_id: UUID, record: NonFileIngestionRecord
    ) -> None:
        async with session.begin():
            selection = await session.scalar(
                select(ResearchSourceSelection)
                .where(ResearchSourceSelection.id == selection_id)
                .with_for_update()
            )
            if selection is None:
                raise AppError(
                    "research_selection_not_found",
                    "Source selection was not found.",
                    status_code=404,
                )
            selection.status = ResearchSourceSelectionStatus.SUCCEEDED.value
            selection.source_id = record.source_id
            selection.source_version_id = record.version_id
            selection.job_id = record.job_id
            selection.error_code = None
            selection.error_message = None
            selection.completed_at = datetime.now(UTC)

    async def _mark_selection_failed(
        self, session: AsyncSession, *, selection_id: UUID, error: AppError
    ) -> None:
        async with session.begin():
            selection = await session.scalar(
                select(ResearchSourceSelection)
                .where(ResearchSourceSelection.id == selection_id)
                .with_for_update()
            )
            if selection is None:
                return
            selection.status = ResearchSourceSelectionStatus.FAILED.value
            selection.error_code = error.code[:100]
            selection.error_message = error.message[:1000]
            selection.completed_at = datetime.now(UTC)

    async def _space(
        self, session: AsyncSession, *, space_id: UUID, for_update: bool = False
    ) -> KnowledgeSpace:
        statement = select(KnowledgeSpace).where(KnowledgeSpace.id == space_id)
        if for_update:
            statement = statement.with_for_update()
        space = await session.scalar(statement)
        if space is None:
            raise AppError(
                "knowledge_space_not_found", "Knowledge space was not found.", status_code=404
            )
        return space

    async def _run(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        research_run_id: UUID,
        for_update: bool = False,
    ) -> ResearchRun:
        statement = select(ResearchRun).where(
            ResearchRun.id == research_run_id, ResearchRun.space_id == space_id
        )
        if for_update:
            statement = statement.with_for_update()
        run = await session.scalar(statement)
        if run is None:
            raise AppError("research_run_not_found", "Research run was not found.", status_code=404)
        return run

    async def _search_outcome(
        self, session: AsyncSession, *, space_id: UUID, research_run_id: UUID
    ) -> SearchRunOutcome:
        run = await self._run(session, space_id=space_id, research_run_id=research_run_id)
        results = tuple(
            (
                await session.scalars(
                    select(ResearchSearchResult)
                    .where(
                        ResearchSearchResult.space_id == space_id,
                        ResearchSearchResult.research_run_id == research_run_id,
                    )
                    .order_by(ResearchSearchResult.ordinal)
                )
            ).all()
        )
        return SearchRunOutcome(run, results)

    async def _batch_outcome(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        research_run_id: UUID,
        batch_id: UUID,
    ) -> SourceSelectionBatchOutcome:
        batch = await session.scalar(
            select(ResearchSourceSelectionBatch).where(
                ResearchSourceSelectionBatch.id == batch_id,
                ResearchSourceSelectionBatch.space_id == space_id,
                ResearchSourceSelectionBatch.research_run_id == research_run_id,
            )
        )
        if batch is None:
            raise AppError(
                "research_selection_batch_not_found",
                "Source selection batch was not found.",
                status_code=404,
            )
        selections = tuple(
            (
                await session.scalars(
                    select(ResearchSourceSelection)
                    .where(ResearchSourceSelection.batch_id == batch_id)
                    .order_by(ResearchSourceSelection.ordinal)
                )
            ).all()
        )
        return SourceSelectionBatchOutcome(batch, selections)


def _normalize_results(response: SearchResponse) -> tuple[Any, ...]:
    normalized = []
    seen: set[str] = set()
    for item in response.results:
        url = normalize_web_url(item.url)
        if url in seen:
            continue
        seen.add(url)
        normalized.append(
            type(item)(
                title=item.title.strip(),
                url=url,
                snippet=item.snippet.strip() if item.snippet else None,
                metadata=dict(item.metadata or {}),
            )
        )
    return tuple(normalized)


def _normalize_selections(
    selections: list[SourceSelectionInput], *, max_urls: int
) -> tuple[SourceSelectionInput, ...]:
    normalized: list[SourceSelectionInput] = []
    seen: set[str] = set()
    for item in selections:
        url = normalize_web_url(item.url)
        if url in seen:
            continue
        seen.add(url)
        normalized.append(SourceSelectionInput(url=url, title=item.title))
    if not normalized:
        raise AppError(
            "empty_source_selection", "At least one unique web URL is required.", status_code=422
        )
    if len(normalized) > max_urls:
        raise AppError(
            "source_selection_limit_exceeded",
            "Source selection exceeds the configured URL limit.",
            status_code=422,
        )
    return tuple(normalized)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
