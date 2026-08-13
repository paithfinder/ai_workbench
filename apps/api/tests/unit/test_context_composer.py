from __future__ import annotations

from uuid import UUID, uuid4

from knowledge_workbench.application.context_composer import ContextComposer
from knowledge_workbench.application.hybrid_retrieval import HybridRetrievalHit
from knowledge_workbench.db.models import RetrievalChunk


def _hit(
    rank: int,
    *,
    chunk_id: UUID | None = None,
    text: str = "evidence",
) -> HybridRetrievalHit:
    chunk = RetrievalChunk()
    chunk.id = chunk_id or uuid4()
    chunk.text = text
    chunk.title = f"Evidence {rank}"
    return HybridRetrievalHit(chunk, rank, 1.0, None, None, rank, 1 / (60 + rank))


def test_context_composer_assigns_stable_evidence_ids_and_respects_limits() -> None:
    hits = [_hit(2, text="second"), _hit(1, text="first-long")]

    context = ContextComposer().compose(
        hits,
        max_chunks=2,
        max_characters=11,
        max_chunk_characters=5,
    )

    assert [item.evidence_id for item in context.evidence] == ["E1", "E2"]
    assert [item.text for item in context.evidence] == ["first", "secon"]
    assert context.total_characters == 10
    assert "[E1] Evidence 1" in context.prompt_text()


def test_context_composer_deduplicates_chunk_id() -> None:
    chunk_id = uuid4()

    context = ContextComposer().compose(
        [_hit(1, chunk_id=chunk_id), _hit(2, chunk_id=chunk_id)],
        max_chunks=8,
        max_characters=100,
        max_chunk_characters=100,
    )

    assert len(context.evidence) == 1
