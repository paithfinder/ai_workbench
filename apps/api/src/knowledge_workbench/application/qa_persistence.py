from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_workbench.application.citation_validation import ValidatedCitation
from knowledge_workbench.application.context_composer import ComposedContext
from knowledge_workbench.application.hybrid_retrieval import HybridRetrievalResult
from knowledge_workbench.application.qa import QaPipelineResult
from knowledge_workbench.application.retrieval import ResolvedScope
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import QaCitation, QaRetrievalHit, QaTurn


def qa_request_hash(
    *, question: str, scope_node_id: UUID | None, include_descendants: bool
) -> str:
    payload = {
        "question": question.strip(),
        "scope_node_id": str(scope_node_id) if scope_node_id is not None else None,
        "include_descendants": include_descendants,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_processing_turn(
    *,
    space_id: UUID,
    idempotency_key: str,
    request_hash: str,
    question: str,
    scope: ResolvedScope,
    retrieval_config: dict[str, object],
    reranker_config: dict[str, object],
    context_config: dict[str, object],
    ai_provider: str,
    ai_model: str,
) -> QaTurn:
    return QaTurn(
        id=uuid4(),
        space_id=space_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        question=question,
        scope_node_id=scope.scope_node_id,
        include_descendants=scope.include_descendants,
        scope_snapshot=scope.snapshot(),
        scope_snapshot_hash=scope.scope_snapshot_hash,
        index_config_version=scope.index_config_version,
        retrieval_config=retrieval_config,
        reranker_config=reranker_config,
        context_config=context_config,
        ai_provider=ai_provider,
        ai_model=ai_model,
        status="processing",
        timings_ms={},
        warnings=[],
    )


async def find_turn_by_key(
    session: AsyncSession, *, space_id: UUID, idempotency_key: str
) -> QaTurn | None:
    return await session.scalar(
        select(QaTurn).where(
            QaTurn.space_id == space_id,
            QaTurn.idempotency_key == idempotency_key,
        )
    )


def ensure_replay_matches(turn: QaTurn, request_hash: str) -> None:
    if turn.request_hash != request_hash:
        raise AppError(
            "idempotency_conflict",
            "Idempotency-Key was already used with a different QA request.",
            status_code=409,
        )


def build_retrieval_rows(
    *,
    turn_id: UUID,
    retrieval: HybridRetrievalResult,
    context: ComposedContext,
) -> tuple[list[QaRetrievalHit], dict[UUID, QaRetrievalHit]]:
    context_by_chunk = {item.hit.chunk.id: item for item in context.evidence}
    rows: list[QaRetrievalHit] = []
    by_chunk: dict[UUID, QaRetrievalHit] = {}
    for hit in retrieval.hits:
        chunk = hit.chunk
        evidence = context_by_chunk.get(chunk.id)
        row = QaRetrievalHit(
            id=uuid4(),
            turn_id=turn_id,
            chunk_id=chunk.id,
            corpus_kind=chunk.corpus_kind,
            content_identity=chunk.content_identity,
            content_hash=chunk.content_hash,
            title=chunk.title,
            chunk_text=chunk.text,
            path=str(chunk.path),
            heading_path=list(chunk.heading_path),
            locator=dict(chunk.locator) if chunk.locator is not None else None,
            knowledge_node_id=chunk.knowledge_node_id,
            knowledge_revision_id=chunk.knowledge_revision_id,
            source_id=chunk.source_id,
            source_version_id=chunk.source_version_id,
            parse_artifact_id=chunk.parse_artifact_id,
            section_id=chunk.section_id,
            keyword_rank=hit.keyword_rank,
            keyword_score=hit.keyword_score,
            vector_rank=hit.vector_rank,
            vector_score=hit.vector_score,
            rrf_rank=hit.rrf_rank,
            rrf_score=hit.rrf_score,
            rerank_rank=hit.rerank_rank,
            rerank_score=hit.rerank_score,
            included_in_context=evidence is not None,
            context_ordinal=evidence.ordinal if evidence is not None else None,
            evidence_id=evidence.evidence_id if evidence is not None else None,
        )
        rows.append(row)
        by_chunk[chunk.id] = row
    return rows, by_chunk


def build_citation_rows(
    *,
    turn_id: UUID,
    citations: list[ValidatedCitation],
    retrieval_rows: dict[UUID, QaRetrievalHit],
) -> list[QaCitation]:
    rows: list[QaCitation] = []
    for citation in citations:
        hit = retrieval_rows.get(citation.chunk_id)
        if hit is None or hit.evidence_id != citation.evidence_id:
            raise ValueError("Citation does not match a persisted context hit")
        rows.append(
            QaCitation(
                id=uuid4(),
                turn_id=turn_id,
                claim_id=citation.claim_id,
                claim_text=citation.claim_text,
                retrieval_hit_id=hit.id,
                evidence_id=citation.evidence_id,
                corpus_kind=citation.corpus_kind,
                content_hash=citation.content_hash,
                knowledge_node_id=citation.knowledge_node_id,
                knowledge_revision_id=citation.knowledge_revision_id,
                source_id=citation.source_id,
                source_version_id=citation.source_version_id,
                parse_artifact_id=citation.parse_artifact_id,
                section_id=citation.section_id,
                frozen_quote=citation.frozen_quote,
                locator=citation.locator,
                deep_link=citation.deep_link,
                validation_status="valid",
            )
        )
    return rows


def apply_pipeline_result(turn: QaTurn, result: QaPipelineResult) -> None:
    turn.status = result.status
    turn.answer = result.answer
    turn.abstain_code = result.abstain_code
    turn.claims = [
        {
            "claim_id": claim.claim_id,
            "claim_text": claim.claim_text,
            "evidence_ids": list(claim.evidence_ids),
        }
        for claim in result.claims
    ]
    turn.warnings = result.warnings
    turn.completed_at = datetime.now(UTC)
    if result.generation is not None:
        turn.ai_provider = result.generation.provider
        turn.ai_model = result.generation.model
        turn.input_tokens = result.generation.usage.input_tokens
        turn.output_tokens = result.generation.usage.output_tokens
        turn.provider_request_id = result.generation.provider_request_id
        turn.timings_ms = {"generation": result.generation.latency_ms}


async def expire_stale_processing_turns(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    timeout_seconds: float,
    limit: int = 100,
) -> int:
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    async with session_factory() as session:
        result = await session.execute(
            update(QaTurn)
            .where(
                QaTurn.id.in_(
                    select(QaTurn.id)
                    .where(QaTurn.status == "processing", QaTurn.started_at < cutoff)
                    .limit(limit)
                )
            )
            .values(
                status="failed",
                error_code="qa_processing_timeout",
                error_message="QA processing did not finish within the configured timeout.",
                completed_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
            .returning(QaTurn.id)
        )
        expired = result.fetchall()
        if expired:
            await session.commit()
        else:
            await session.rollback()
        return len(expired)


def apply_failure(turn: QaTurn, *, code: str, message: str) -> None:
    turn.status = "failed"
    turn.answer = None
    turn.abstain_code = None
    turn.claims = []
    turn.error_code = code
    turn.error_message = message[:2000]
    turn.completed_at = datetime.now(UTC)
