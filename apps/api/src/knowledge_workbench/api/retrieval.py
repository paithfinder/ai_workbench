from __future__ import annotations

from datetime import datetime
from time import perf_counter
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.application.debug_retrieval import DebugRetrievalService, RetrievalHit
from knowledge_workbench.application.indexing import IndexingService
from knowledge_workbench.application.ports.embedding_gateway import EmbeddingError
from knowledge_workbench.application.retrieval import ResolvedScope, ScopeRequest, ScopeResolver
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    RetrievalChunk,
    RetrievalIndexRun,
    Source,
    SourceVersion,
)
from knowledge_workbench.worker.source_index import create_embedding_gateway

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}/retrieval", tags=["retrieval"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
IndexState = Literal["ready", "building", "degraded", "failed", "empty"]
CorpusKind = Literal["source_evidence", "confirmed_knowledge"]
RebuildState = Literal["queued", "running", "succeeded", "failed"]


def _default_channels() -> list[Literal["keyword", "vector"]]:
    return ["keyword", "vector"]


class ScopePreviewRequest(BaseModel):
    scope_node_id: UUID | None = None
    include_descendants: bool = True


class ScopeSummaryResponse(BaseModel):
    scope_node_id: UUID
    include_descendants: bool
    scope_snapshot_hash: str
    scope_path: str
    node_kind: str
    node_title: str
    knowledge_count: int
    source_count: int
    source_version_count: int
    chunk_count: int
    index_status: IndexState
    index_config_version: str | None


class ScopePreviewResponse(BaseModel):
    scope_summary: ScopeSummaryResponse


class DebugRetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    scope: ScopePreviewRequest
    top_k: int = Field(default=5, ge=1, le=100)
    channels: list[Literal["keyword", "vector"]] = Field(
        default_factory=_default_channels, min_length=1, max_length=2
    )


class DebugRetrievalHitResponse(BaseModel):
    chunk_id: UUID
    content_identity: str
    rank: int
    raw_score: float
    score_semantics: str
    corpus_kind: CorpusKind
    knowledge_node_id: UUID | None
    knowledge_revision_id: UUID | None
    knowledge_path: list[str]
    source_id: UUID | None
    source_title: str | None
    source_version_id: UUID | None
    source_version_number: int | None
    parse_artifact_id: UUID | None
    section_id: UUID | None
    snippet: str
    deep_link: str | None


class EmbeddingResponse(BaseModel):
    provider: str
    model: str
    dimensions: int


class DebugRetrievalResponse(BaseModel):
    query: str
    scope_summary: ScopeSummaryResponse
    embedding: EmbeddingResponse | None
    keyword_hits: list[DebugRetrievalHitResponse]
    vector_hits: list[DebugRetrievalHitResponse]
    channel_errors: dict[str, str | None]
    timings_ms: dict[str, float]


class ChannelStatusResponse(BaseModel):
    status: IndexState
    indexed_chunks: int


class VectorStatusResponse(ChannelStatusResponse):
    provider: str | None
    model: str | None
    dimensions: int | None


class IndexStatusResponse(BaseModel):
    status: IndexState
    index_version: str | None
    indexed_chunks: int
    pending_chunks: int
    keyword: ChannelStatusResponse
    vector: VectorStatusResponse
    updated_at: datetime | None


class IndexRebuildResponse(BaseModel):
    rebuild_id: UUID
    status: RebuildState
    message: str


def _scope_summary(scope: ResolvedScope) -> ScopeSummaryResponse:
    return ScopeSummaryResponse.model_validate(scope.snapshot())


async def _knowledge_path(
    session: SessionDependency, *, scope: ResolvedScope, chunk: RetrievalChunk
) -> list[str]:
    breadcrumb = await ScopeResolver._breadcrumb(
        session,
        space_id=scope.space_id,
        node_path=str(chunk.path),
        fallback=chunk.title or "",
    )
    return [part.strip() for part in breadcrumb.split("/") if part.strip()]


async def _hits(
    session: SessionDependency,
    *,
    scope: ResolvedScope,
    hits: list[RetrievalHit],
    semantics: str,
) -> list[DebugRetrievalHitResponse]:
    source_version_ids = {
        hit.chunk.source_version_id
        for hit in hits
        if hit.chunk.source_version_id is not None
    }
    source_rows = (
        await session.execute(
            select(SourceVersion.id, SourceVersion.version_number, Source.title)
            .join(Source, Source.id == SourceVersion.source_id)
            .where(SourceVersion.id.in_(source_version_ids))
        )
    ).all() if source_version_ids else []
    source_metadata = {
        version_id: (version_number, source_title)
        for version_id, version_number, source_title in source_rows
    }
    items: list[DebugRetrievalHitResponse] = []
    for hit in hits:
        chunk = hit.chunk
        source_version_number: int | None = None
        source_title: str | None = None
        if chunk.source_version_id is not None:
            metadata = source_metadata.get(chunk.source_version_id)
            if metadata is not None:
                source_version_number, source_title = metadata
        deep_link = None
        if (
            chunk.source_id is not None
            and chunk.source_version_id is not None
            and chunk.parse_artifact_id is not None
            and chunk.section_id is not None
        ):
            deep_link = (
                f"/sources/{chunk.source_id}?versionId={chunk.source_version_id}"
                f"&artifactId={chunk.parse_artifact_id}&sectionId={chunk.section_id}"
            )
        items.append(
            DebugRetrievalHitResponse(
                chunk_id=chunk.id,
                content_identity=chunk.content_identity,
                rank=hit.rank,
                raw_score=hit.score,
                score_semantics=semantics,
                corpus_kind=cast(CorpusKind, chunk.corpus_kind),
                knowledge_node_id=chunk.knowledge_node_id,
                knowledge_revision_id=chunk.knowledge_revision_id,
                knowledge_path=await _knowledge_path(session, scope=scope, chunk=chunk),
                source_id=chunk.source_id,
                source_title=source_title,
                source_version_id=chunk.source_version_id,
                source_version_number=source_version_number,
                parse_artifact_id=chunk.parse_artifact_id,
                section_id=chunk.section_id,
                snippet=chunk.text,
                deep_link=deep_link,
            )
        )
    return items


def _require_debug(settings: SettingsDependency) -> None:
    if settings.app_env not in {"development", "test"}:
        raise AppError("retrieval_debug_disabled", "Retrieval debug is disabled.", status_code=404)


@router.post(
    "/scope-preview", response_model=ScopePreviewResponse, operation_id="preview_retrieval_scope"
)
async def scope_preview(
    space_id: UUID,
    body: ScopePreviewRequest,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ScopePreviewResponse:
    _require_debug(settings)
    scope = await ScopeResolver().resolve(
        session,
        settings=settings,
        space_id=space_id,
        request=ScopeRequest(body.scope_node_id, body.include_descendants),
    )
    return ScopePreviewResponse(scope_summary=_scope_summary(scope))


@router.post(
    "/debug-search",
    response_model=DebugRetrievalResponse,
    operation_id="debug_retrieval_search",
)
async def debug_search(
    space_id: UUID,
    body: DebugRetrievalRequest,
    session: SessionDependency,
    settings: SettingsDependency,
) -> DebugRetrievalResponse:
    _require_debug(settings)
    normalized_query = body.query.strip()
    if not normalized_query:
        raise AppError(
            "invalid_retrieval_query",
            "Retrieval query must not be blank.",
            status_code=422,
        )
    channels = list(dict.fromkeys(body.channels))
    scope_started = perf_counter()
    scope = await ScopeResolver().resolve(
        session,
        settings=settings,
        space_id=space_id,
        request=ScopeRequest(body.scope.scope_node_id, body.scope.include_descendants),
    )
    timings = {"scope": (perf_counter() - scope_started) * 1000}
    service = DebugRetrievalService()
    keyword_hits: list[DebugRetrievalHitResponse] = []
    vector_hits: list[DebugRetrievalHitResponse] = []
    errors: dict[str, str | None] = {"keyword": None, "vector": None}
    embedding: EmbeddingResponse | None = None
    if "keyword" in channels:
        started = perf_counter()
        hits = await service.keyword(
            session,
            settings=settings,
            scope=scope,
            query=normalized_query,
            top_k=body.top_k,
        )
        keyword_hits = await _hits(
            session,
            scope=scope,
            hits=hits,
            semantics="PostgreSQL ts_rank_cd (higher is better)",
        )
        timings["keyword"] = (perf_counter() - started) * 1000
    if "vector" in channels:
        started = perf_counter()
        try:
            gateway = create_embedding_gateway(settings)
            hits = await service.vector(
                session,
                settings=settings,
                scope=scope,
                query=normalized_query,
                top_k=body.top_k,
                gateway=gateway,
            )
            vector_hits = await _hits(
                session,
                scope=scope,
                hits=hits,
                semantics="1 - cosine distance (higher is better)",
            )
            embedding = EmbeddingResponse(
                provider=settings.embedding_provider,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
            )
        except EmbeddingError as exc:
            errors["vector"] = str(exc) or "Vector embedding is unavailable."
        timings["vector"] = (perf_counter() - started) * 1000
    return DebugRetrievalResponse(
        query=normalized_query,
        scope_summary=_scope_summary(scope),
        embedding=embedding,
        keyword_hits=keyword_hits,
        vector_hits=vector_hits,
        channel_errors=errors,
        timings_ms=timings,
    )


@router.get(
    "/index-status",
    response_model=IndexStatusResponse,
    operation_id="get_retrieval_index_status",
)
async def index_status(
    space_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> IndexStatusResponse:
    scope = await ScopeResolver().resolve(
        session,
        settings=settings,
        space_id=space_id,
        request=ScopeRequest(None, True),
    )
    pending = int(
        await session.scalar(
            select(func.count())
            .select_from(RetrievalIndexRun)
            .where(
                RetrievalIndexRun.space_id == space_id,
                RetrievalIndexRun.index_config_version == settings.index_version,
                RetrievalIndexRun.status.in_(("queued", "running")),
            )
        )
        or 0
    )
    updated_at = await session.scalar(
        select(func.max(RetrievalIndexRun.completed_at)).where(
            RetrievalIndexRun.space_id == space_id,
            RetrievalIndexRun.index_config_version == settings.index_version,
        )
    )
    return IndexStatusResponse(
        status=cast(IndexState, scope.index_status),
        index_version=scope.index_config_version,
        indexed_chunks=scope.chunk_count,
        pending_chunks=pending,
        keyword=ChannelStatusResponse(
            status=cast(IndexState, scope.index_status),
            indexed_chunks=scope.chunk_count,
        ),
        vector=VectorStatusResponse(
            status=cast(IndexState, scope.index_status),
            indexed_chunks=scope.chunk_count,
            provider=settings.embedding_provider if scope.chunk_count else None,
            model=settings.embedding_model if scope.chunk_count else None,
            dimensions=settings.embedding_dimensions if scope.chunk_count else None,
        ),
        updated_at=updated_at,
    )


@router.post(
    "/index-rebuilds",
    response_model=IndexRebuildResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="rebuild_retrieval_index",
)
async def index_rebuilds(
    space_id: UUID,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> IndexRebuildResponse:
    async with session.begin():
        outcomes = await IndexingService().request_space_rebuild(
            session,
            settings=settings,
            space_id=space_id,
            idempotency_key=idempotency_key,
        )
    if not outcomes:
        raise AppError(
            "index_rebuild_empty",
            "The knowledge space has no indexable content.",
            status_code=409,
        )
    first = outcomes[0]
    statuses = {"ready": "succeeded", "queued": "queued", "running": "running", "failed": "failed"}
    return IndexRebuildResponse(
        rebuild_id=first.run.id,
        status=cast(RebuildState, statuses[first.run.status]),
        message=f"Queued {len(outcomes)} retrieval index target(s).",
    )
