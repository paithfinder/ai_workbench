from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.context_composer import ContextComposer
from knowledge_workbench.application.hybrid_retrieval import HybridRetrievalService
from knowledge_workbench.application.knowledge_update_proposal import (
    KnowledgeUpdateProposalService,
    ProposalCreate,
    ProposalEvidenceInput,
)
from knowledge_workbench.application.proposal_comparison_generation import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ProposalComparisonGenerationOutput,
    ProposalComparisonGenerator,
)
from knowledge_workbench.application.retrieval import ScopeRequest, ScopeResolver
from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    ActivityEvent,
    ActorType,
    KnowledgeEvidence,
    KnowledgeNode,
    KnowledgeUpdateAction,
    KnowledgeUpdateProposalEvidenceRole,
    ParseArtifactStatus,
    ProposalComparisonCandidate,
    ProposalComparisonCandidateKind,
    ProposalComparisonKind,
    ProposalComparisonRun,
    ProposalComparisonRunStatus,
    ResearchRun,
    ResearchRunStatus,
    ResearchSourceSelection,
    ResearchSourceSelectionBatch,
    ResearchSourceSelectionBatchStatus,
    ResearchSourceSelectionStatus,
    Source,
    SourceParseArtifact,
    SourceSection,
    SourceVersion,
)
from knowledge_workbench.infrastructure.ai.embedding_factory import (
    create_embedding_gateway,
)
from knowledge_workbench.infrastructure.ai.proposal_comparison_factory import (
    create_proposal_comparison_gateway,
)
from knowledge_workbench.infrastructure.ai.reranker_factory import create_reranker_gateway


class ProposalComparisonScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_node_id: UUID | None = None
    include_descendants: bool = True


class ProposalComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_batch_id: UUID
    scope: ProposalComparisonScope


@dataclass(frozen=True, slots=True)
class ProposalComparisonOutcome:
    run: ProposalComparisonRun
    candidates: list[ProposalComparisonCandidate]


def proposal_comparison_request_hash(
    *, research_run_id: UUID, request: ProposalComparisonRequest
) -> str:
    payload = {
        "research_run_id": str(research_run_id),
        "selection_batch_id": str(request.selection_batch_id),
        "scope": request.scope.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ProposalComparisonService:
    def __init__(
        self,
        *,
        retrieval: HybridRetrievalService | None = None,
        composer: ContextComposer | None = None,
        generator: ProposalComparisonGenerator | None = None,
        proposals: KnowledgeUpdateProposalService | None = None,
    ) -> None:
        self._retrieval = retrieval or HybridRetrievalService()
        self._composer = composer or ContextComposer()
        self._generator = generator or ProposalComparisonGenerator()
        self._proposals = proposals or KnowledgeUpdateProposalService()

    async def compare(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        space_id: UUID,
        research_run_id: UUID,
        idempotency_key: str,
        request: ProposalComparisonRequest,
    ) -> ProposalComparisonOutcome:
        key = _validate_idempotency_key(idempotency_key)
        request_hash = proposal_comparison_request_hash(
            research_run_id=research_run_id, request=request
        )
        await session.execute(
            select(
                func.pg_advisory_xact_lock(func.hashtext(f"proposal-comparison:{space_id}:{key}"))
            )
        )
        replay = await self._replay(
            session,
            settings=settings,
            space_id=space_id,
            key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        scope = await ScopeResolver().resolve(
            session,
            settings=settings,
            space_id=space_id,
            request=ScopeRequest(request.scope.scope_node_id, request.scope.include_descendants),
        )
        await self._validate_run_and_batch(
            session,
            space_id=space_id,
            research_run_id=research_run_id,
            selection_batch_id=request.selection_batch_id,
        )
        run = ProposalComparisonRun(
            id=uuid4(),
            space_id=space_id,
            research_run_id=research_run_id,
            selection_batch_id=request.selection_batch_id,
            idempotency_key=key,
            request_hash=request_hash,
            scope_node_id=scope.scope_node_id,
            include_descendants=scope.include_descendants,
            scope_snapshot=scope.snapshot(),
            scope_snapshot_hash=scope.scope_snapshot_hash,
            index_config_version=scope.index_config_version,
            retrieval_config={
                "candidate_top_k": settings.proposal_comparison_candidate_top_k,
                "rrf_k": settings.proposal_comparison_rrf_k,
            },
            reranker_config={
                "provider": settings.reranker_provider,
                "model": settings.reranker_model,
            },
            context_config={
                "max_chunks": settings.proposal_comparison_context_max_chunks,
                "max_characters": settings.proposal_comparison_context_max_characters,
                "max_chunk_characters": settings.proposal_comparison_context_max_chunk_characters,
            },
            ai_provider=settings.ai_provider,
            ai_model=settings.claude_model if settings.ai_provider == "anthropic" else "fake",
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            status=ProposalComparisonRunStatus.PROCESSING.value,
        )
        session.add(run)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            replay = await self._replay(
                session,
                settings=settings,
                space_id=space_id,
                key=key,
                request_hash=request_hash,
            )
            if replay is None:
                raise
            return replay

        try:
            candidates, warnings = await self._build_candidates(
                session,
                settings=settings,
                run=run,
                scope=scope,
            )
            candidate_context = self._apply_context_budget(
                candidates,
                max_chunks=settings.proposal_comparison_context_max_chunks,
                max_characters=settings.proposal_comparison_context_max_characters,
                max_chunk_characters=settings.proposal_comparison_context_max_chunk_characters,
            )
            session.add_all(candidates)
            run.warnings = warnings
            await session.commit()
            if not candidates:
                await self._complete_no_evidence(
                    session,
                    run=run,
                    warning="no_comparable_selected_source_sections",
                )
                return await self._outcome(session, run)
            generation = await asyncio.wait_for(
                self._generator.generate(
                    create_proposal_comparison_gateway(settings),
                    candidate_prompt=self._candidate_prompt(candidates, candidate_context),
                ),
                timeout=settings.proposal_comparison_processing_timeout_seconds,
            )
            output = generation.value
            run.input_tokens = generation.usage.input_tokens
            run.output_tokens = generation.usage.output_tokens
            run.provider_request_id = generation.provider_request_id
            run.timings_ms = {"generation": generation.latency_ms}
            if output.comparison_kind == ProposalComparisonKind.NO_EVIDENCE.value:
                await self._complete_no_evidence(
                    session, run=run, warning=output.uncertainty_reason
                )
                return await self._outcome(session, run)
            proposal_request = await self._validated_proposal_request(
                session,
                run=run,
                output=output,
                scope_path=scope.scope_ltree,
            )
            outcome = await self._proposals.create_comparison_draft(
                session,
                space_id=space_id,
                comparison_run_id=run.id,
                request=proposal_request,
            )
            run.status = ProposalComparisonRunStatus.COMPLETED.value
            run.comparison_kind = output.comparison_kind
            run.proposal_id = outcome.proposal_id
            run.completed_at = datetime.now(UTC)
            session.add(
                ActivityEvent(
                    id=uuid4(),
                    space_id=space_id,
                    event_type="proposal_comparison.completed",
                    entity_type="proposal_comparison_run",
                    entity_id=run.id,
                    actor_type=ActorType.USER.value,
                    payload={
                        "proposal_id": str(outcome.proposal_id),
                        "kind": output.comparison_kind,
                    },
                )
            )
            await session.commit()
            return await self._outcome(session, run)
        except Exception as exc:
            await session.rollback()
            await self._mark_failed(session, run_id=run.id, space_id=space_id, exc=exc)
            if isinstance(exc, AppError):
                raise
            raise AppError(
                "proposal_comparison_failed",
                "Proposal comparison could not be completed.",
                status_code=502,
            ) from exc

    async def get(
        self, session: AsyncSession, *, space_id: UUID, comparison_id: UUID
    ) -> ProposalComparisonOutcome:
        run = await session.scalar(
            select(ProposalComparisonRun).where(
                ProposalComparisonRun.id == comparison_id,
                ProposalComparisonRun.space_id == space_id,
            )
        )
        if run is None:
            raise AppError(
                "proposal_comparison_not_found",
                "Proposal comparison was not found.",
                status_code=404,
            )
        return await self._outcome(session, run)

    async def _replay(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        space_id: UUID,
        key: str,
        request_hash: str,
    ) -> ProposalComparisonOutcome | None:
        run = await session.scalar(
            select(ProposalComparisonRun).where(
                ProposalComparisonRun.space_id == space_id,
                ProposalComparisonRun.idempotency_key == key,
            )
        )
        if run is None:
            return None
        if run.request_hash != request_hash:
            raise AppError(
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request.",
                status_code=409,
            )
        if run.status == ProposalComparisonRunStatus.PROCESSING.value:
            expiry = datetime.now(UTC) - timedelta(
                seconds=settings.proposal_comparison_processing_timeout_seconds
            )
            if run.started_at < expiry:
                await self._mark_failed(
                    session,
                    run_id=run.id,
                    space_id=space_id,
                    exc=AppError(
                        "proposal_comparison_processing_timeout",
                        "Comparison processing timed out.",
                        status_code=409,
                    ),
                )
            else:
                raise AppError(
                    "proposal_comparison_processing",
                    "Proposal comparison is still processing.",
                    status_code=409,
                )
        return await self._outcome(session, run)

    async def _validate_run_and_batch(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        research_run_id: UUID,
        selection_batch_id: UUID,
    ) -> None:
        row = await session.execute(
            select(ResearchRun, ResearchSourceSelectionBatch)
            .join(
                ResearchSourceSelectionBatch,
                ResearchSourceSelectionBatch.research_run_id == ResearchRun.id,
            )
            .where(
                ResearchRun.id == research_run_id,
                ResearchRun.space_id == space_id,
                ResearchRun.status == ResearchRunStatus.COMPLETED.value,
                ResearchSourceSelectionBatch.id == selection_batch_id,
                ResearchSourceSelectionBatch.space_id == space_id,
                ResearchSourceSelectionBatch.status.in_(
                    [
                        ResearchSourceSelectionBatchStatus.COMPLETED.value,
                        ResearchSourceSelectionBatchStatus.COMPLETED_WITH_FAILURES.value,
                    ]
                ),
            )
        )
        if row.one_or_none() is None:
            raise AppError(
                "proposal_comparison_input_invalid",
                "Research run and selection batch must be completed in this knowledge space.",
                status_code=422,
            )

    async def _build_candidates(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        run: ProposalComparisonRun,
        scope: Any,
    ) -> tuple[list[ProposalComparisonCandidate], list[str]]:
        selected = list(
            (
                await session.execute(
                    select(
                        ResearchSourceSelection,
                        Source,
                        SourceVersion,
                        SourceParseArtifact,
                        SourceSection,
                    )
                    .join(Source, Source.id == ResearchSourceSelection.source_id)
                    .join(
                        SourceVersion, SourceVersion.id == ResearchSourceSelection.source_version_id
                    )
                    .join(
                        SourceParseArtifact,
                        SourceParseArtifact.id == SourceVersion.current_parse_artifact_id,
                    )
                    .join(
                        SourceSection,
                        (SourceSection.source_version_id == SourceVersion.id)
                        & (SourceSection.parse_artifact_id == SourceParseArtifact.id),
                    )
                    .where(
                        ResearchSourceSelection.batch_id == run.selection_batch_id,
                        ResearchSourceSelection.research_run_id == run.research_run_id,
                        ResearchSourceSelection.space_id == run.space_id,
                        ResearchSourceSelection.status
                        == ResearchSourceSelectionStatus.SUCCEEDED.value,
                        Source.space_id == run.space_id,
                        Source.status == "active",
                        Source.deleted_at.is_(None),
                        SourceVersion.parse_status == "ready",
                        SourceParseArtifact.status == ParseArtifactStatus.READY.value,
                        SourceSection.space_id == run.space_id,
                    )
                    .order_by(
                        ResearchSourceSelection.ordinal, SourceSection.ordinal, SourceSection.id
                    )
                )
            ).all()
        )
        candidates: list[ProposalComparisonCandidate] = []
        warnings: list[str] = []
        for selection, source, version, artifact, section in selected:
            candidates.append(
                self._new_candidate(
                    run=run,
                    ordinal=len(candidates),
                    candidate_number=sum(
                        item.candidate_kind == ProposalComparisonCandidateKind.NEW_SOURCE.value
                        for item in candidates
                    )
                    + 1,
                    selection=selection,
                    source=source,
                    version=version,
                    artifact=artifact,
                    section=section,
                )
            )
            retrieval = await self._retrieval.retrieve_confirmed_knowledge(
                session,
                settings=settings,
                scope=scope,
                query=section.text,
                embedding_gateway=create_embedding_gateway(settings),
                reranker_gateway=create_reranker_gateway(settings),
                candidate_top_k=settings.proposal_comparison_candidate_top_k,
                rrf_k=settings.proposal_comparison_rrf_k,
            )
            warnings.extend(retrieval.warnings)
            context = self._composer.compose(
                retrieval.hits,
                max_chunks=settings.proposal_comparison_context_max_chunks,
                max_characters=settings.proposal_comparison_context_max_characters,
                max_chunk_characters=settings.proposal_comparison_context_max_chunk_characters,
            )
            for context_item in context.evidence:
                existing_candidates = await self._existing_candidates_for_chunk(
                    session,
                    run=run,
                    chunk=context_item.hit,
                    included_in_context=True,
                    context_ordinal=context_item.ordinal,
                    ordinal_start=len(candidates),
                )
                for candidate in existing_candidates:
                    if any(
                        item.knowledge_evidence_id == candidate.knowledge_evidence_id
                        for item in candidates
                    ):
                        continue
                    candidate.ordinal = len(candidates)
                    existing_number = sum(
                        item.candidate_kind
                        == ProposalComparisonCandidateKind.EXISTING_EVIDENCE.value
                        for item in candidates
                    )
                    candidate.candidate_id = f"E{existing_number + 1}"
                    candidates.append(candidate)
        return candidates, list(dict.fromkeys(warnings))

    @staticmethod
    def _new_candidate(
        *,
        run: ProposalComparisonRun,
        ordinal: int,
        candidate_number: int,
        selection: ResearchSourceSelection,
        source: Source,
        version: SourceVersion,
        artifact: SourceParseArtifact,
        section: SourceSection,
    ) -> ProposalComparisonCandidate:
        return ProposalComparisonCandidate(
            id=uuid4(),
            comparison_run_id=run.id,
            space_id=run.space_id,
            candidate_id=f"N{candidate_number}",
            ordinal=ordinal,
            candidate_kind=ProposalComparisonCandidateKind.NEW_SOURCE.value,
            selection_id=selection.id,
            source_id=source.id,
            source_version_id=version.id,
            parse_artifact_id=artifact.id,
            section_id=section.id,
            title=source.title,
            frozen_quote=section.text,
            quote_hash=section.quote_hash,
            content_hash=section.content_hash,
            locator=dict(section.locator),
        )

    async def _existing_candidates_for_chunk(
        self,
        session: AsyncSession,
        *,
        run: ProposalComparisonRun,
        chunk: Any,
        included_in_context: bool,
        context_ordinal: int,
        ordinal_start: int,
    ) -> list[ProposalComparisonCandidate]:
        rows = list(
            (
                await session.execute(
                    select(
                        KnowledgeEvidence,
                        KnowledgeNode,
                        Source,
                        SourceVersion,
                        SourceParseArtifact,
                        SourceSection,
                    )
                    .join(
                        KnowledgeNode,
                        KnowledgeNode.current_revision_id == KnowledgeEvidence.revision_id,
                    )
                    .join(SourceVersion, SourceVersion.id == KnowledgeEvidence.source_version_id)
                    .join(Source, Source.id == SourceVersion.source_id)
                    .join(
                        SourceParseArtifact,
                        SourceParseArtifact.id == SourceVersion.current_parse_artifact_id,
                    )
                    .join(SourceSection, SourceSection.id == KnowledgeEvidence.section_id)
                    .where(
                        KnowledgeEvidence.space_id == run.space_id,
                        KnowledgeEvidence.revision_id == chunk.chunk.knowledge_revision_id,
                        KnowledgeNode.id == chunk.chunk.knowledge_node_id,
                        KnowledgeNode.space_id == run.space_id,
                        KnowledgeNode.deleted_at.is_(None),
                        Source.space_id == run.space_id,
                        Source.status == "active",
                        Source.deleted_at.is_(None),
                        SourceVersion.parse_status == "ready",
                        SourceSection.space_id == run.space_id,
                        SourceParseArtifact.status == ParseArtifactStatus.READY.value,
                        SourceSection.parse_artifact_id == SourceParseArtifact.id,
                        SourceSection.source_version_id == SourceVersion.id,
                        KnowledgeEvidence.parse_artifact_id == SourceParseArtifact.id,
                        KnowledgeEvidence.source_version_id == SourceVersion.id,
                        KnowledgeEvidence.quote_hash == SourceSection.quote_hash,
                        KnowledgeEvidence.content_hash == SourceSection.content_hash,
                        KnowledgeEvidence.locator == SourceSection.locator,
                        KnowledgeEvidence.frozen_quote == SourceSection.text,
                    )
                    .order_by(KnowledgeEvidence.id)
                )
            ).all()
        )
        result: list[ProposalComparisonCandidate] = []
        for offset, (evidence, node, source, version, artifact, section) in enumerate(rows):
            result.append(
                ProposalComparisonCandidate(
                    id=uuid4(),
                    comparison_run_id=run.id,
                    space_id=run.space_id,
                    candidate_id=f"E{ordinal_start + offset + 1}",
                    ordinal=ordinal_start + offset,
                    candidate_kind=ProposalComparisonCandidateKind.EXISTING_EVIDENCE.value,
                    source_id=source.id,
                    source_version_id=version.id,
                    parse_artifact_id=artifact.id,
                    section_id=section.id,
                    knowledge_node_id=node.id,
                    knowledge_revision_id=evidence.revision_id,
                    knowledge_evidence_id=evidence.id,
                    title=source.title,
                    frozen_quote=section.text,
                    quote_hash=section.quote_hash,
                    content_hash=section.content_hash,
                    locator=dict(section.locator),
                    keyword_rank=chunk.keyword_rank,
                    keyword_score=chunk.keyword_score,
                    vector_rank=chunk.vector_rank,
                    vector_score=chunk.vector_score,
                    rrf_rank=chunk.rrf_rank,
                    rrf_score=chunk.rrf_score,
                    rerank_rank=chunk.rerank_rank,
                    rerank_score=chunk.rerank_score,
                    included_in_context=included_in_context,
                    context_ordinal=context_ordinal,
                )
            )
        return result

    async def _validated_proposal_request(
        self,
        session: AsyncSession,
        *,
        run: ProposalComparisonRun,
        output: ProposalComparisonGenerationOutput,
        scope_path: str,
    ) -> ProposalCreate:
        candidates = list(
            await session.scalars(
                select(ProposalComparisonCandidate)
                .where(ProposalComparisonCandidate.comparison_run_id == run.id)
                .order_by(ProposalComparisonCandidate.ordinal)
            )
        )
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        new = self._candidate_ids(
            output.new_evidence_ids, by_id, ProposalComparisonCandidateKind.NEW_SOURCE
        )
        existing = self._candidate_ids(
            output.existing_evidence_ids,
            by_id,
            ProposalComparisonCandidateKind.EXISTING_EVIDENCE,
        )
        if set(output.new_evidence_ids) & set(output.existing_evidence_ids):
            raise AppError(
                "proposal_comparison_evidence_invalid",
                "Candidate IDs cannot be reused.",
                status_code=409,
            )
        await self._revalidate_new_candidates(session, run=run, candidates=new)
        await self._revalidate_existing_candidates(
            session,
            run=run,
            scope_path=scope_path,
            candidates=existing,
        )
        target_node_id = existing[0].knowledge_node_id if existing else None
        target_revision_id = existing[0].knowledge_revision_id if existing else None
        action = self._action(output.comparison_kind, output.suggested_action, bool(existing))
        if action != KnowledgeUpdateAction.CREATE and target_node_id is None:
            raise AppError(
                "proposal_comparison_evidence_invalid",
                "An existing target is required.",
                status_code=409,
            )
        evidence = [
            ProposalEvidenceInput(
                role=KnowledgeUpdateProposalEvidenceRole.NEW_SUPPORT, section_id=item.section_id
            )
            for item in new
        ]
        existing_role = self._existing_role(output.comparison_kind)
        evidence.extend(
            ProposalEvidenceInput(
                role=existing_role,
                section_id=item.section_id,
                knowledge_revision_id=item.knowledge_revision_id,
            )
            for item in existing
        )
        return ProposalCreate(
            action=action,
            research_run_id=run.research_run_id,
            target_node_id=target_node_id,
            target_revision_id=target_revision_id,
            suggested_title=output.suggested_title,
            suggested_body=output.suggested_body,
            suggested_tags=output.suggested_tags,
            conditions=output.conditions,
            exceptions=output.exceptions,
            comparison_summary=output.summary,
            confidence=output.confidence,
            uncertainty_reason=output.uncertainty_reason,
            evidence=evidence,
        )

    @staticmethod
    def _candidate_ids(
        identifiers: list[str],
        candidates: dict[str, ProposalComparisonCandidate],
        expected_kind: ProposalComparisonCandidateKind,
    ) -> list[ProposalComparisonCandidate]:
        if len(set(identifiers)) != len(identifiers):
            raise AppError(
                "proposal_comparison_evidence_invalid",
                "Candidate IDs must be unique.",
                status_code=409,
            )
        resolved = [candidates.get(identifier) for identifier in identifiers]
        if any(
            candidate is None or candidate.candidate_kind != expected_kind.value
            for candidate in resolved
        ):
            raise AppError(
                "proposal_comparison_evidence_invalid",
                "Candidate IDs are invalid for this role.",
                status_code=409,
            )
        return [candidate for candidate in resolved if candidate is not None]

    async def _revalidate_new_candidates(
        self,
        session: AsyncSession,
        *,
        run: ProposalComparisonRun,
        candidates: list[ProposalComparisonCandidate],
    ) -> None:
        for candidate in candidates:
            valid = await session.scalar(
                select(ResearchSourceSelection.id)
                .join(Source, Source.id == ResearchSourceSelection.source_id)
                .join(
                    SourceVersion,
                    SourceVersion.id == ResearchSourceSelection.source_version_id,
                )
                .join(
                    SourceParseArtifact,
                    SourceParseArtifact.id == SourceVersion.current_parse_artifact_id,
                )
                .join(SourceSection, SourceSection.id == candidate.section_id)
                .where(
                    ResearchSourceSelection.id == candidate.selection_id,
                    ResearchSourceSelection.batch_id == run.selection_batch_id,
                    ResearchSourceSelection.research_run_id == run.research_run_id,
                    ResearchSourceSelection.space_id == run.space_id,
                    ResearchSourceSelection.status == ResearchSourceSelectionStatus.SUCCEEDED.value,
                    ResearchSourceSelection.source_id == candidate.source_id,
                    ResearchSourceSelection.source_version_id == candidate.source_version_id,
                    Source.id == candidate.source_id,
                    Source.space_id == run.space_id,
                    Source.status == "active",
                    Source.deleted_at.is_(None),
                    SourceVersion.parse_status == "ready",
                    SourceParseArtifact.id == candidate.parse_artifact_id,
                    SourceParseArtifact.status == ParseArtifactStatus.READY.value,
                    SourceSection.source_version_id == candidate.source_version_id,
                    SourceSection.parse_artifact_id == candidate.parse_artifact_id,
                    SourceSection.quote_hash == candidate.quote_hash,
                    SourceSection.content_hash == candidate.content_hash,
                    SourceSection.text == candidate.frozen_quote,
                    SourceSection.locator == candidate.locator,
                )
            )
            if valid is None:
                raise AppError(
                    "proposal_comparison_evidence_invalid",
                    "New evidence is no longer valid.",
                    status_code=409,
                )

    async def _revalidate_existing_candidates(
        self,
        session: AsyncSession,
        *,
        run: ProposalComparisonRun,
        scope_path: str,
        candidates: list[ProposalComparisonCandidate],
    ) -> None:
        for candidate in candidates:
            valid = await session.scalar(
                select(KnowledgeEvidence.id)
                .join(
                    KnowledgeNode,
                    KnowledgeNode.current_revision_id == KnowledgeEvidence.revision_id,
                )
                .join(SourceVersion, SourceVersion.id == KnowledgeEvidence.source_version_id)
                .join(Source, Source.id == SourceVersion.source_id)
                .join(
                    SourceParseArtifact,
                    SourceParseArtifact.id == SourceVersion.current_parse_artifact_id,
                )
                .join(SourceSection, SourceSection.id == KnowledgeEvidence.section_id)
                .where(
                    KnowledgeEvidence.id == candidate.knowledge_evidence_id,
                    KnowledgeEvidence.space_id == run.space_id,
                    KnowledgeEvidence.revision_id == candidate.knowledge_revision_id,
                    KnowledgeEvidence.source_version_id == candidate.source_version_id,
                    KnowledgeEvidence.parse_artifact_id == candidate.parse_artifact_id,
                    KnowledgeEvidence.section_id == candidate.section_id,
                    KnowledgeEvidence.quote_hash == candidate.quote_hash,
                    KnowledgeEvidence.content_hash == candidate.content_hash,
                    KnowledgeEvidence.locator == candidate.locator,
                    KnowledgeEvidence.frozen_quote == candidate.frozen_quote,
                    KnowledgeNode.id == candidate.knowledge_node_id,
                    KnowledgeNode.space_id == run.space_id,
                    KnowledgeNode.deleted_at.is_(None),
                    KnowledgeNode.path.op("<@")(scope_path)
                    if run.include_descendants
                    else KnowledgeNode.path == scope_path,
                    Source.id == candidate.source_id,
                    Source.space_id == run.space_id,
                    Source.status == "active",
                    Source.deleted_at.is_(None),
                    SourceVersion.parse_status == "ready",
                    SourceParseArtifact.status == ParseArtifactStatus.READY.value,
                    SourceSection.source_version_id == candidate.source_version_id,
                    SourceSection.parse_artifact_id == candidate.parse_artifact_id,
                    SourceSection.quote_hash == candidate.quote_hash,
                    SourceSection.content_hash == candidate.content_hash,
                    SourceSection.text == candidate.frozen_quote,
                    SourceSection.locator == candidate.locator,
                )
            )
            if valid is None:
                raise AppError(
                    "proposal_comparison_evidence_invalid",
                    "Existing evidence is no longer valid.",
                    status_code=409,
                )

    @staticmethod
    def _action(
        kind: str, requested: KnowledgeUpdateAction | None, has_existing: bool
    ) -> KnowledgeUpdateAction:
        if kind == ProposalComparisonKind.DUPLICATE.value:
            return KnowledgeUpdateAction.MARK_REVIEW_RECOMMENDED
        if kind in {ProposalComparisonKind.CONFLICT.value, ProposalComparisonKind.OUTDATED.value}:
            return KnowledgeUpdateAction.REVISE if has_existing else KnowledgeUpdateAction.CREATE
        return requested or KnowledgeUpdateAction.CREATE

    @staticmethod
    def _existing_role(kind: str) -> KnowledgeUpdateProposalEvidenceRole:
        if kind == ProposalComparisonKind.CONFLICT.value:
            return KnowledgeUpdateProposalEvidenceRole.CONFLICT
        if kind == ProposalComparisonKind.OUTDATED.value:
            return KnowledgeUpdateProposalEvidenceRole.OUTDATED
        return KnowledgeUpdateProposalEvidenceRole.EXISTING_SUPPORT

    @staticmethod
    def _apply_context_budget(
        candidates: list[ProposalComparisonCandidate],
        *,
        max_chunks: int,
        max_characters: int,
        max_chunk_characters: int,
    ) -> dict[str, str]:
        context: dict[str, str] = {}
        total = 0
        for item in candidates:
            item.included_in_context = False
            item.context_ordinal = None
            if len(context) >= max_chunks:
                continue
            remaining = max_characters - total
            if remaining <= 0:
                break
            text = item.frozen_quote[: min(max_chunk_characters, remaining)].strip()
            if not text:
                continue
            item.included_in_context = True
            item.context_ordinal = len(context)
            context[item.candidate_id] = text
            total += len(text)
        return context

    @staticmethod
    def _candidate_prompt(
        candidates: list[ProposalComparisonCandidate], context: dict[str, str]
    ) -> str:
        blocks = []
        for item in candidates:
            text = context.get(item.candidate_id)
            if text is None:
                continue
            header = f"[{item.candidate_id}] kind={item.candidate_kind}; title={item.title or ''}"
            blocks.append(f"{header}\n{text}")
        return "候选证据：\n\n" + "\n\n".join(blocks)

    async def _complete_no_evidence(
        self, session: AsyncSession, *, run: ProposalComparisonRun, warning: str | None
    ) -> None:
        run.status = ProposalComparisonRunStatus.NO_EVIDENCE.value
        run.comparison_kind = ProposalComparisonKind.NO_EVIDENCE.value
        run.completed_at = datetime.now(UTC)
        if warning:
            run.warnings = list(dict.fromkeys([*run.warnings, warning]))
        session.add(
            ActivityEvent(
                id=uuid4(),
                space_id=run.space_id,
                event_type="proposal_comparison.no_evidence",
                entity_type="proposal_comparison_run",
                entity_id=run.id,
                actor_type=ActorType.USER.value,
                payload={"kind": ProposalComparisonKind.NO_EVIDENCE.value},
            )
        )
        await session.commit()

    async def _mark_failed(
        self, session: AsyncSession, *, run_id: UUID, space_id: UUID, exc: Exception
    ) -> None:
        run = await session.scalar(
            select(ProposalComparisonRun)
            .where(ProposalComparisonRun.id == run_id, ProposalComparisonRun.space_id == space_id)
            .with_for_update()
        )
        if run is None or run.status != ProposalComparisonRunStatus.PROCESSING.value:
            return
        run.status = ProposalComparisonRunStatus.FAILED.value
        run.failure_code = (
            exc.code if isinstance(exc, AppError) else "proposal_comparison_generation_failed"
        )
        run.failure_message = "Proposal comparison could not be completed."
        run.completed_at = datetime.now(UTC)
        session.add(
            ActivityEvent(
                id=uuid4(),
                space_id=space_id,
                event_type="proposal_comparison.failed",
                entity_type="proposal_comparison_run",
                entity_id=run.id,
                actor_type=ActorType.USER.value,
                payload={"failure_code": run.failure_code},
            )
        )
        await session.commit()

    async def _outcome(
        self, session: AsyncSession, run: ProposalComparisonRun
    ) -> ProposalComparisonOutcome:
        candidates = list(
            await session.scalars(
                select(ProposalComparisonCandidate)
                .where(ProposalComparisonCandidate.comparison_run_id == run.id)
                .order_by(ProposalComparisonCandidate.ordinal)
            )
        )
        return ProposalComparisonOutcome(run=run, candidates=candidates)
