from __future__ import annotations

import math
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.ports.embedding_gateway import (
    EmbeddingError,
    EmbeddingGateway,
)
from knowledge_workbench.application.ports.reranker_gateway import (
    RerankDocument,
    RerankerError,
    RerankerGateway,
)
from knowledge_workbench.application.retrieval import ResolvedScope
from knowledge_workbench.application.retrieval_channels import RetrievalHit, RetrievalService
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import RetrievalChunk


@dataclass(frozen=True, slots=True)
class HybridRetrievalHit:
    chunk: RetrievalChunk
    keyword_rank: int | None
    keyword_score: float | None
    vector_rank: int | None
    vector_score: float | None
    rrf_rank: int
    rrf_score: float
    rerank_rank: int | None = None
    rerank_score: float | None = None

    @property
    def final_rank(self) -> int:
        return self.rerank_rank or self.rrf_rank


@dataclass(frozen=True, slots=True)
class HybridRetrievalResult:
    hits: list[HybridRetrievalHit]
    warnings: list[str]


def reciprocal_rank_fusion(
    keyword_hits: list[RetrievalHit],
    vector_hits: list[RetrievalHit],
    *,
    rrf_k: int,
) -> list[HybridRetrievalHit]:
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive")
    fused: dict[UUID, HybridRetrievalHit] = {}
    for channel, hits in (("keyword", keyword_hits), ("vector", vector_hits)):
        seen: set[UUID] = set()
        for hit in hits:
            chunk_id = hit.chunk.id
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            current = fused.get(chunk_id)
            contribution = 1.0 / (rrf_k + hit.rank)
            if current is None:
                current = HybridRetrievalHit(
                    chunk=hit.chunk,
                    keyword_rank=None,
                    keyword_score=None,
                    vector_rank=None,
                    vector_score=None,
                    rrf_rank=0,
                    rrf_score=0.0,
                )
            if channel == "keyword":
                current = replace(
                    current,
                    keyword_rank=hit.rank,
                    keyword_score=hit.score,
                    rrf_score=current.rrf_score + contribution,
                )
            else:
                current = replace(
                    current,
                    vector_rank=hit.rank,
                    vector_score=hit.score,
                    rrf_score=current.rrf_score + contribution,
                )
            fused[chunk_id] = current
    ordered = sorted(
        fused.values(),
        key=lambda hit: (
            -hit.rrf_score,
            min(rank for rank in (hit.keyword_rank, hit.vector_rank) if rank is not None),
            hit.chunk.id,
        ),
    )
    return [replace(hit, rrf_rank=rank) for rank, hit in enumerate(ordered, 1)]


def apply_reranker_scores(
    hits: list[HybridRetrievalHit],
    scores: dict[UUID, float],
) -> list[HybridRetrievalHit]:
    expected = {hit.chunk.id for hit in hits}
    if set(scores) != expected or not all(math.isfinite(score) for score in scores.values()):
        raise ValueError("Reranker scores must match candidates and be finite")
    ordered = sorted(hits, key=lambda hit: (-scores[hit.chunk.id], hit.rrf_rank, hit.chunk.id))
    return [
        replace(hit, rerank_rank=rank, rerank_score=scores[hit.chunk.id])
        for rank, hit in enumerate(ordered, 1)
    ]


class HybridRetrievalService:
    def __init__(self, channels: RetrievalService | None = None) -> None:
        self._channels = channels or RetrievalService()

    async def retrieve_confirmed_knowledge(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        scope: ResolvedScope,
        query: str,
        embedding_gateway: EmbeddingGateway,
        reranker_gateway: RerankerGateway | None,
        candidate_top_k: int,
        rrf_k: int,
    ) -> HybridRetrievalResult:
        warnings: list[str] = []
        keyword_hits = await self._channels.confirmed_knowledge_keyword(
            session,
            settings=settings,
            scope=scope,
            query=query,
            top_k=candidate_top_k,
        )
        await session.commit()
        try:
            vector_hits = await self._channels.confirmed_knowledge_vector(
                session,
                settings=settings,
                scope=scope,
                query=query,
                top_k=candidate_top_k,
                gateway=embedding_gateway,
            )
        except EmbeddingError as exc:
            vector_hits = []
            warnings.append(f"vector_unavailable: {exc}")
        else:
            await session.commit()
        fused = reciprocal_rank_fusion(keyword_hits, vector_hits, rrf_k=rrf_k)
        if reranker_gateway is None or not fused:
            return HybridRetrievalResult(fused, warnings)
        try:
            reranked = await reranker_gateway.rerank(
                query=query,
                documents=[RerankDocument(hit.chunk.id, hit.chunk.text) for hit in fused],
            )
            scores = {item.id: item.score for item in reranked}
            if len(scores) != len(reranked):
                raise ValueError("Reranker returned duplicate candidate identities")
            fused = apply_reranker_scores(fused, scores)
        except (RerankerError, ValueError) as exc:
            warnings.append(f"reranker_fallback: {exc}")
        return HybridRetrievalResult(fused, warnings)

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        scope: ResolvedScope,
        query: str,
        embedding_gateway: EmbeddingGateway,
        reranker_gateway: RerankerGateway | None,
    ) -> HybridRetrievalResult:
        warnings: list[str] = []
        keyword_hits = await self._channels.keyword(
            session,
            settings=settings,
            scope=scope,
            query=query,
            top_k=settings.qa_candidate_top_k,
        )
        await session.commit()
        try:
            vector_hits = await self._channels.vector(
                session,
                settings=settings,
                scope=scope,
                query=query,
                top_k=settings.qa_candidate_top_k,
                gateway=embedding_gateway,
            )
        except EmbeddingError as exc:
            vector_hits = []
            warnings.append(f"vector_unavailable: {exc}")
        else:
            await session.commit()
        fused = reciprocal_rank_fusion(
            keyword_hits,
            vector_hits,
            rrf_k=settings.qa_rrf_k,
        )
        if reranker_gateway is None or not fused:
            return HybridRetrievalResult(fused, warnings)
        try:
            reranked = await reranker_gateway.rerank(
                query=query,
                documents=[RerankDocument(hit.chunk.id, hit.chunk.text) for hit in fused],
            )
            scores = {item.id: item.score for item in reranked}
            if len(scores) != len(reranked):
                raise ValueError("Reranker returned duplicate candidate identities")
            fused = apply_reranker_scores(fused, scores)
        except (RerankerError, ValueError) as exc:
            warnings.append(f"reranker_fallback: {exc}")
        return HybridRetrievalResult(fused, warnings)
