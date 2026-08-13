from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.citation_validation import (
    AnswerClaim,
    CitationValidator,
    ValidatedCitation,
)
from knowledge_workbench.application.context_composer import ComposedContext, ContextComposer
from knowledge_workbench.application.hybrid_retrieval import HybridRetrievalResult
from knowledge_workbench.application.ports.ai_gateway import AIGateway, StructuredGenerationResult
from knowledge_workbench.application.qa_generation import QaGenerationOutput, QaGenerator
from knowledge_workbench.application.retrieval import ResolvedScope
from knowledge_workbench.config import Settings


@dataclass(frozen=True, slots=True)
class QaPipelineResult:
    status: str
    context: ComposedContext
    answer: str | None
    abstain_code: str | None
    claims: list[AnswerClaim]
    citations: list[ValidatedCitation]
    generation: StructuredGenerationResult[QaGenerationOutput] | None
    warnings: list[str]


class QaPipeline:
    def __init__(
        self,
        *,
        composer: ContextComposer | None = None,
        generator: QaGenerator | None = None,
        validator: CitationValidator | None = None,
    ) -> None:
        self._composer = composer or ContextComposer()
        self._generator = generator or QaGenerator()
        self._validator = validator or CitationValidator()

    async def answer(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        scope: ResolvedScope,
        question: str,
        retrieval: HybridRetrievalResult,
        gateway: AIGateway,
    ) -> QaPipelineResult:
        context = self._composer.compose(
            retrieval.hits,
            max_chunks=settings.qa_context_max_chunks,
            max_characters=settings.qa_context_max_characters,
            max_chunk_characters=settings.qa_context_max_chunk_characters,
        )
        if not context.evidence:
            return QaPipelineResult(
                status="abstained",
                context=context,
                answer=None,
                abstain_code="no_evidence",
                claims=[],
                citations=[],
                generation=None,
                warnings=retrieval.warnings,
            )
        generation = await self._generator.generate(
            gateway,
            question=question,
            context=context,
        )
        output = generation.value
        await self._validator.validate_context(
            session,
            settings=settings,
            scope=scope,
            context=context,
        )
        if output.status == "abstained":
            return QaPipelineResult(
                status="abstained",
                context=context,
                answer=None,
                abstain_code=output.abstain_code,
                claims=[],
                citations=[],
                generation=generation,
                warnings=retrieval.warnings,
            )
        claims = output.answer_claims()
        citations = self._validator.validate_claims(
            claims=claims,
            context=context,
        )
        return QaPipelineResult(
            status="answered",
            context=context,
            answer=output.rendered_answer(),
            abstain_code=None,
            claims=claims,
            citations=citations,
            generation=generation,
            warnings=retrieval.warnings,
        )
