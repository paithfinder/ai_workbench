from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.application.citation_validation import CitationValidationError
from knowledge_workbench.application.hybrid_retrieval import (
    HybridRetrievalResult,
    HybridRetrievalService,
)
from knowledge_workbench.application.ports.ai_gateway import AIGatewayError
from knowledge_workbench.application.ports.embedding_gateway import EmbeddingError
from knowledge_workbench.application.qa import QaPipeline, QaPipelineResult
from knowledge_workbench.application.qa_persistence import (
    apply_failure,
    apply_pipeline_result,
    build_citation_rows,
    build_retrieval_rows,
    create_processing_turn,
    ensure_replay_matches,
    find_turn_by_key,
    qa_request_hash,
)
from knowledge_workbench.application.retrieval import ScopeRequest, ScopeResolver
from knowledge_workbench.core.errors import AppError, ErrorEnvelope
from knowledge_workbench.db.models import QaCitation, QaRetrievalHit, QaTurn
from knowledge_workbench.infrastructure.ai.embedding_factory import create_embedding_gateway
from knowledge_workbench.infrastructure.ai.qa_factory import create_qa_gateway
from knowledge_workbench.infrastructure.ai.reranker_factory import create_reranker_gateway

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}/qa", tags=["qa"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
QA_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 422, 500)
}


class QaScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_node_id: UUID | None = None
    include_descendants: bool = True


class QaTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    scope: QaScopeRequest = Field(default_factory=QaScopeRequest)


class QaClaimResponse(BaseModel):
    claim_id: str
    claim_text: str
    evidence_ids: list[str]


class QaCitationResponse(BaseModel):
    claim_id: str
    claim_text: str
    evidence_id: str
    content_identity: str
    section_id: UUID | None
    corpus_kind: Literal["source_evidence", "confirmed_knowledge"]
    frozen_quote: str
    deep_link: str | None


class QaTurnResponse(BaseModel):
    id: UUID
    status: Literal["processing", "answered", "abstained", "failed"]
    question: str
    scope_snapshot: dict[str, object]
    index_config_version: str | None
    ai_provider: str
    ai_model: str
    answer: str | None
    abstain_code: str | None
    error_code: str | None
    error_message: str | None
    warnings: list[str]
    claims: list[QaClaimResponse]
    citations: list[QaCitationResponse]


async def _expire_stale_processing_turn(
    session: SessionDependency,
    turn: QaTurn,
    *,
    timeout_seconds: float,
) -> QaTurn:
    if turn.status != "processing":
        return turn
    started_at = turn.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - started_at <= timedelta(seconds=timeout_seconds):
        return turn
    locked = await session.scalar(
        select(QaTurn).where(QaTurn.id == turn.id).with_for_update()
    )
    if locked is None or locked.status != "processing":
        return locked or turn
    apply_failure(
        locked,
        code="qa_processing_timeout",
        message="QA processing did not finish within the configured timeout.",
    )
    await session.commit()
    return locked


async def _response(session: SessionDependency, turn: QaTurn) -> QaTurnResponse:
    citation_rows = (
        await session.execute(
            select(QaCitation, QaRetrievalHit)
            .join(QaRetrievalHit, QaRetrievalHit.id == QaCitation.retrieval_hit_id)
            .where(QaCitation.turn_id == turn.id)
            .order_by(QaCitation.claim_id, QaCitation.evidence_id)
        )
    ).all()
    return QaTurnResponse(
        id=turn.id,
        status=turn.status,  # type: ignore[arg-type]
        question=turn.question,
        scope_snapshot=turn.scope_snapshot,
        index_config_version=turn.index_config_version,
        ai_provider=turn.ai_provider,
        ai_model=turn.ai_model,
        answer=turn.answer,
        abstain_code=turn.abstain_code,
        error_code=turn.error_code,
        error_message=turn.error_message,
        warnings=turn.warnings,
        claims=[QaClaimResponse.model_validate(claim) for claim in turn.claims],
        citations=[
            QaCitationResponse(
                claim_id=citation.claim_id,
                claim_text=citation.claim_text,
                evidence_id=citation.evidence_id,
                content_identity=hit.content_identity,
                section_id=hit.section_id,
                corpus_kind=citation.corpus_kind,  # type: ignore[arg-type]
                frozen_quote=citation.frozen_quote,
                deep_link=citation.deep_link,
            )
            for citation, hit in citation_rows
        ],
    )


@router.post(
    "/turns",
    response_model=QaTurnResponse,
    operation_id="create_qa_turn",
    responses=QA_ERROR_RESPONSES,
)
async def create_turn(
    space_id: UUID,
    body: QaTurnRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> QaTurnResponse:
    question = body.question.strip()
    if not question:
        raise AppError("invalid_qa_question", "QA question must not be blank.", status_code=422)
    key = idempotency_key.strip()
    if not key:
        raise AppError("invalid_idempotency_key", "Idempotency-Key is required.", status_code=422)
    request_hash = qa_request_hash(
        question=question,
        scope_node_id=body.scope.scope_node_id,
        include_descendants=body.scope.include_descendants,
    )
    existing = await find_turn_by_key(session, space_id=space_id, idempotency_key=key)
    if existing is not None:
        ensure_replay_matches(existing, request_hash)
        existing = await _expire_stale_processing_turn(
            session,
            existing,
            timeout_seconds=settings.qa_processing_timeout_seconds,
        )
        return await _response(session, existing)
    scope = await ScopeResolver().resolve(
        session,
        settings=settings,
        space_id=space_id,
        request=ScopeRequest(body.scope.scope_node_id, body.scope.include_descendants),
    )
    turn = create_processing_turn(
        space_id=space_id,
        idempotency_key=key,
        request_hash=request_hash,
        question=question,
        scope=scope,
        retrieval_config={
            "candidate_top_k": settings.qa_candidate_top_k,
            "rrf_k": settings.qa_rrf_k,
        },
        reranker_config={
            "provider": settings.reranker_provider,
            "model": settings.reranker_model,
        },
        context_config={
            "max_chunks": settings.qa_context_max_chunks,
            "max_characters": settings.qa_context_max_characters,
            "max_chunk_characters": settings.qa_context_max_chunk_characters,
        },
        ai_provider=settings.ai_provider,
        ai_model=settings.claude_model if settings.ai_provider == "anthropic" else "fake",
    )
    session.add(turn)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        replay = await find_turn_by_key(session, space_id=space_id, idempotency_key=key)
        if replay is None:
            raise
        ensure_replay_matches(replay, request_hash)
        replay = await _expire_stale_processing_turn(
            session,
            replay,
            timeout_seconds=settings.qa_processing_timeout_seconds,
        )
        return await _response(session, replay)
    try:
        async def run_pipeline() -> tuple[HybridRetrievalResult, QaPipelineResult]:
            retrieval_result = await HybridRetrievalService().retrieve(
                session,
                settings=settings,
                scope=scope,
                query=question,
                embedding_gateway=create_embedding_gateway(settings),
                reranker_gateway=create_reranker_gateway(settings),
            )
            await session.commit()
            pipeline_result = await QaPipeline().answer(
                session,
                settings=settings,
                scope=scope,
                question=question,
                retrieval=retrieval_result,
                gateway=create_qa_gateway(settings),
            )
            return retrieval_result, pipeline_result

        retrieval, result = await asyncio.wait_for(
            run_pipeline(),
            timeout=settings.qa_processing_timeout_seconds,
        )
        locked = await session.scalar(select(QaTurn).where(QaTurn.id == turn.id).with_for_update())
        if locked is None:
            raise RuntimeError("QA turn disappeared during processing")
        if locked.status != "processing":
            await session.rollback()
            return await _response(session, locked)
        retrieval_rows, rows_by_chunk = build_retrieval_rows(
            turn_id=locked.id,
            retrieval=retrieval,
            context=result.context,
        )
        session.add_all(retrieval_rows)
        session.add_all(
            build_citation_rows(
                turn_id=locked.id,
                citations=result.citations,
                retrieval_rows=rows_by_chunk,
            )
        )
        apply_pipeline_result(locked, result)
        await session.commit()
        return await _response(session, locked)
    except (
        AIGatewayError,
        CitationValidationError,
        EmbeddingError,
        TimeoutError,
    ) as exc:
        await session.rollback()
        locked = await session.scalar(
            select(QaTurn).where(QaTurn.id == turn.id).with_for_update()
        )
        if locked is None:
            raise RuntimeError("QA turn disappeared while recording failure") from exc
        if locked.status != "processing":
            await session.rollback()
            return await _response(session, locked)
        failure_code = (
            "qa_processing_timeout"
            if isinstance(exc, TimeoutError)
            else "qa_generation_failed"
        )
        apply_failure(locked, code=failure_code, message=str(exc) or "QA processing timed out")
        await session.commit()
        return await _response(session, locked)


@router.get(
    "/turns/{turn_id}",
    response_model=QaTurnResponse,
    operation_id="get_qa_turn",
    responses={
        404: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    },
)
async def get_turn(
    space_id: UUID,
    turn_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> QaTurnResponse:
    turn = await session.scalar(
        select(QaTurn).where(QaTurn.id == turn_id, QaTurn.space_id == space_id)
    )
    if turn is None:
        raise AppError("qa_turn_not_found", "QA turn was not found.", status_code=404)
    turn = await _expire_stale_processing_turn(
        session,
        turn,
        timeout_seconds=settings.qa_processing_timeout_seconds,
    )
    return await _response(session, turn)


@router.get(
    "/turns/by-idempotency-key/{idempotency_key}",
    response_model=QaTurnResponse,
    operation_id="get_qa_turn_by_idempotency_key",
    responses={
        404: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    },
)
async def get_turn_by_key(
    space_id: UUID,
    idempotency_key: str,
    session: SessionDependency,
    settings: SettingsDependency,
) -> QaTurnResponse:
    turn = await find_turn_by_key(
        session,
        space_id=space_id,
        idempotency_key=idempotency_key,
    )
    if turn is None:
        raise AppError("qa_turn_not_found", "QA turn was not found.", status_code=404)
    turn = await _expire_stale_processing_turn(
        session,
        turn,
        timeout_seconds=settings.qa_processing_timeout_seconds,
    )
    return await _response(session, turn)
