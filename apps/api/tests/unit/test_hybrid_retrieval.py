from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from knowledge_workbench.application.hybrid_retrieval import (
    HybridRetrievalService,
    apply_reranker_scores,
    reciprocal_rank_fusion,
)
from knowledge_workbench.application.ports.embedding_gateway import (
    EmbeddingUnavailableError,
)
from knowledge_workbench.application.ports.reranker_gateway import (
    RerankerUnavailableError,
)
from knowledge_workbench.application.retrieval import ResolvedScope
from knowledge_workbench.application.retrieval_channels import RetrievalHit
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import RetrievalChunk


def _chunk(chunk_id: UUID | None = None, *, text: str = "evidence") -> RetrievalChunk:
    chunk = RetrievalChunk()
    chunk.id = chunk_id or uuid4()
    chunk.text = text
    return chunk


def test_rrf_deduplicates_chunks_and_preserves_channel_metadata() -> None:
    shared = _chunk()
    keyword_only = _chunk()
    vector_only = _chunk()

    hits = reciprocal_rank_fusion(
        [RetrievalHit(shared, 1, 0.9), RetrievalHit(keyword_only, 2, 0.7)],
        [RetrievalHit(vector_only, 1, 0.8), RetrievalHit(shared, 2, 0.6)],
        rrf_k=60,
    )

    assert [hit.chunk.id for hit in hits] == [shared.id, vector_only.id, keyword_only.id]
    assert hits[0].keyword_rank == 1
    assert hits[0].keyword_score == 0.9
    assert hits[0].vector_rank == 2
    assert hits[0].vector_score == 0.6
    assert [hit.rrf_rank for hit in hits] == [1, 2, 3]


def test_rrf_ignores_duplicate_chunk_within_a_channel() -> None:
    chunk = _chunk()

    hits = reciprocal_rank_fusion(
        [RetrievalHit(chunk, 1, 0.9), RetrievalHit(chunk, 2, 0.8)],
        [],
        rrf_k=60,
    )

    assert len(hits) == 1
    assert hits[0].keyword_rank == 1
    assert hits[0].rrf_score == 1 / 61


def test_rrf_ties_are_stable_by_best_rank_then_chunk_id() -> None:
    first = _chunk(UUID(int=1))
    second = _chunk(UUID(int=2))

    hits = reciprocal_rank_fusion(
        [RetrievalHit(second, 1, 1.0)],
        [RetrievalHit(first, 1, 1.0)],
        rrf_k=60,
    )

    assert [hit.chunk.id for hit in hits] == [first.id, second.id]


def test_apply_reranker_scores_preserves_rrf_as_tie_break() -> None:
    chunks = [_chunk(), _chunk()]
    fused = reciprocal_rank_fusion(
        [RetrievalHit(chunks[0], 1, 1.0), RetrievalHit(chunks[1], 2, 0.5)],
        [],
        rrf_k=60,
    )

    reranked = apply_reranker_scores(fused, {chunk.id: 0.5 for chunk in chunks})

    assert [hit.chunk.id for hit in reranked] == [chunks[0].id, chunks[1].id]
    assert [hit.rerank_rank for hit in reranked] == [1, 2]


async def test_hybrid_service_preserves_keyword_when_vector_and_reranker_fail() -> None:
    chunk = _chunk()
    channels = AsyncMock()
    channels.keyword.return_value = [RetrievalHit(chunk, 1, 0.8)]
    channels.vector.side_effect = EmbeddingUnavailableError("embedding offline")
    reranker = AsyncMock()
    reranker.rerank.side_effect = RerankerUnavailableError("reranker offline")

    session = AsyncMock()
    result = await HybridRetrievalService(channels).retrieve(
        session,
        settings=Settings(app_env="test"),  # type: ignore[arg-type]
        scope=ResolvedScope(
            space_id=uuid4(),
            scope_node_id=uuid4(),
            scope_ltree="n1",
            scope_path="测试范围",
            node_kind="folder",
            node_title="测试范围",
            include_descendants=True,
            knowledge_count=1,
            source_count=0,
            source_version_count=0,
            chunk_count=1,
            index_status="ready",
            index_config_version="d7-v1",
            scope_snapshot_hash="a" * 64,
        ),
        query="question",
        embedding_gateway=AsyncMock(),
        reranker_gateway=reranker,
    )

    assert [hit.chunk.id for hit in result.hits] == [chunk.id]
    session.commit.assert_awaited_once()
    assert result.hits[0].rerank_rank is None
    assert result.warnings == [
        "vector_unavailable: embedding offline",
        "reranker_fallback: reranker offline",
    ]
