from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from knowledge_workbench.application.hybrid_retrieval import HybridRetrievalResult
from knowledge_workbench.application.qa import QaPipeline
from knowledge_workbench.config import Settings
from knowledge_workbench.infrastructure.ai.fake import FakeAIGateway


async def test_qa_pipeline_abstains_without_calling_ai_for_empty_retrieval() -> None:
    gateway = FakeAIGateway()
    validator = AsyncMock()
    pipeline = QaPipeline(validator=validator)

    result = await pipeline.answer(
        object(),  # type: ignore[arg-type]
        settings=Settings(app_env="test"),  # type: ignore[arg-type]
        scope=AsyncMock(),
        question="问题",
        retrieval=HybridRetrievalResult([], ["vector_unavailable: offline"]),
        gateway=gateway,
    )

    assert result.status == "abstained"
    assert result.abstain_code == "no_evidence"
    assert result.generation is None
    assert result.warnings == ["vector_unavailable: offline"]
    assert gateway.calls == []
    validator.validate_context.assert_not_awaited()


async def test_qa_pipeline_publishes_answer_only_after_citation_validation() -> None:
    from knowledge_workbench.application.citation_validation import ValidatedCitation
    from knowledge_workbench.application.context_composer import ComposedContext, ContextEvidence
    from knowledge_workbench.application.hybrid_retrieval import HybridRetrievalHit
    from knowledge_workbench.application.qa_generation import QaGenerationOutput
    from knowledge_workbench.db.models import RetrievalChunk

    chunk = RetrievalChunk()
    chunk.id = uuid4()
    chunk.text = "证据"
    hit = HybridRetrievalHit(chunk, 1, 1.0, None, None, 1, 0.01)
    context = ComposedContext([ContextEvidence("E1", 0, hit, "证据")], 2)
    composer = Mock()
    composer.compose.return_value = context
    validator = Mock()
    validator.validate_context = AsyncMock()
    citation = ValidatedCitation(
        claim_id="C1",
        claim_text="事实",
        evidence_id="E1",
        chunk_id=chunk.id,
        corpus_kind="confirmed_knowledge",
        content_hash="a" * 64,
        frozen_quote="证据",
        locator=None,
        deep_link="/knowledge/1",
        knowledge_node_id=uuid4(),
        knowledge_revision_id=uuid4(),
        source_id=None,
        source_version_id=None,
        parse_artifact_id=None,
        section_id=None,
    )
    validator.validate_claims.return_value = [citation]
    gateway = FakeAIGateway(
        {
            "answer_qa_turn": lambda _: QaGenerationOutput.model_validate(
                {
                    "status": "answered",
                    "answer": "回答",
                    "claims": [
                        {"claim_id": "C1", "claim_text": "事实", "evidence_ids": ["E1"]}
                    ],
                }
            )
        }
    )

    result = await QaPipeline(composer=composer, validator=validator).answer(
        object(),  # type: ignore[arg-type]
        settings=Settings(app_env="test"),  # type: ignore[arg-type]
        scope=AsyncMock(),
        question="问题",
        retrieval=HybridRetrievalResult([hit], []),
        gateway=gateway,
    )

    assert result.status == "answered"
    assert result.answer == "事实"
    assert [claim.claim_id for claim in result.claims] == ["C1"]
    assert result.citations == [citation]
    validator.validate_context.assert_awaited_once()
    validator.validate_claims.assert_called_once()
