from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.ports.embedding_gateway import EmbeddingGateway
from knowledge_workbench.application.retrieval import ResolvedScope, ScopeResolver
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import RetrievalChunk


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk: RetrievalChunk
    rank: int
    score: float


class RetrievalService:
    async def keyword(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        scope: ResolvedScope,
        query: str,
        top_k: int,
    ) -> list[RetrievalHit]:
        tsquery = func.retrieval_fts_query(query)
        score = func.ts_rank_cd(RetrievalChunk.search_vector, tsquery).label("score")
        predicates = ScopeResolver.scope_predicates(
            settings=settings,
            space_id=scope.space_id,
            scope_path=scope.scope_ltree,
            include_descendants=scope.include_descendants,
        )
        rows = (
            await session.execute(
                select(RetrievalChunk, score)
                .where(*predicates, RetrievalChunk.search_vector.op("@@")(tsquery))
                .order_by(score.desc(), RetrievalChunk.id)
                .limit(top_k)
            )
        ).all()
        return [
            RetrievalHit(chunk, rank, float(value))
            for rank, (chunk, value) in enumerate(rows, 1)
        ]

    async def vector(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        scope: ResolvedScope,
        query: str,
        top_k: int,
        gateway: EmbeddingGateway,
    ) -> list[RetrievalHit]:
        embedded = await gateway.embed([query])
        return await self.vector_with_embedding(
            session,
            settings=settings,
            scope=scope,
            vector=embedded.vectors[0],
            top_k=top_k,
        )

    async def vector_with_embedding(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        scope: ResolvedScope,
        vector: list[float],
        top_k: int,
    ) -> list[RetrievalHit]:
        distance = RetrievalChunk.embedding.cosine_distance(vector)
        score = (1 - distance).label("score")
        predicates = ScopeResolver.scope_predicates(
            settings=settings,
            space_id=scope.space_id,
            scope_path=scope.scope_ltree,
            include_descendants=scope.include_descendants,
        )
        rows = (
            await session.execute(
                select(RetrievalChunk, score)
                .where(*predicates)
                .order_by(distance, RetrievalChunk.id)
                .limit(top_k)
            )
        ).all()
        return [
            RetrievalHit(chunk, rank, float(value))
            for rank, (chunk, value) in enumerate(rows, 1)
        ]
