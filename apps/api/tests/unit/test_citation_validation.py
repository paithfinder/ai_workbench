from __future__ import annotations

from uuid import uuid4

import pytest

from knowledge_workbench.application.citation_validation import (
    AnswerClaim,
    CitationValidationError,
    CitationValidator,
)
from knowledge_workbench.application.context_composer import ComposedContext, ContextComposer
from knowledge_workbench.application.hybrid_retrieval import HybridRetrievalHit
from knowledge_workbench.db.models import RetrievalChunk


def _source_hit() -> HybridRetrievalHit:
    chunk = RetrievalChunk()
    chunk.id = uuid4()
    chunk.text = "冻结的来源证据"
    chunk.title = "来源"
    chunk.content_hash = "a" * 64
    chunk.corpus_kind = "source_evidence"
    chunk.source_id = uuid4()
    chunk.source_version_id = uuid4()
    chunk.parse_artifact_id = uuid4()
    chunk.section_id = uuid4()
    chunk.knowledge_node_id = None
    chunk.knowledge_revision_id = None
    chunk.locator = {"page": 2}
    return HybridRetrievalHit(chunk, 1, 0.8, 1, 0.9, 1, 0.03)


def _context() -> ComposedContext:
    return ContextComposer().compose(
        [_source_hit()],
        max_chunks=8,
        max_characters=1000,
        max_chunk_characters=1000,
    )


def test_citation_validator_freezes_valid_source_identity() -> None:
    context = _context()

    citations = CitationValidator().validate_claims(
        claims=[AnswerClaim("C1", "事实", ["E1"])],
        context=context,
    )

    assert len(citations) == 1
    assert citations[0].evidence_id == "E1"
    assert citations[0].frozen_quote == "冻结的来源证据"
    assert citations[0].deep_link.endswith(f"sectionId={citations[0].section_id}")


@pytest.mark.parametrize(
    "claims",
    [
        [],
        [AnswerClaim("C1", "事实", [])],
        [AnswerClaim("C1", "事实", ["E9"])],
        [AnswerClaim("C1", "事实", ["E1"]), AnswerClaim("C1", "另一事实", ["E1"])],
    ],
)
def test_citation_validator_fails_closed_for_uncovered_or_unknown_claims(
    claims: list[AnswerClaim],
) -> None:
    with pytest.raises(CitationValidationError):
        CitationValidator().validate_claims(
            claims=claims,
            context=_context(),
        )
