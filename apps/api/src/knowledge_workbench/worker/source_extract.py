from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_workbench.application.extraction import (
    CandidateDraft,
    ExtractionBatchOutput,
    build_extraction_batches,
    candidate_fingerprint,
    extraction_system_prompt,
    extraction_user_message,
)
from knowledge_workbench.application.ports.ai_gateway import (
    AIGateway,
    AIGatewayError,
    AIInvalidOutputError,
    AIMessage,
    AIRateLimitedError,
    AIRefusalError,
    AITimeoutError,
    AIUnavailableError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    CandidateEvidence,
    CandidateStatus,
    ExtractionCandidate,
    ExtractionJob,
    ExtractionStatus,
    Job,
    JobAttempt,
    JobAttemptStatus,
    JobKind,
    JobStatus,
    Source,
    SourceSection,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine, create_session_factory
from knowledge_workbench.infrastructure.ai.claude import ClaudeAIGateway
from knowledge_workbench.infrastructure.ai.fake import FakeAIGateway
from knowledge_workbench.worker.job_runner import ClaimToken, JobRunner, PermanentJobError
from knowledge_workbench.worker.job_runner import (
    mark_transient_failure as mark_job_transient_failure,
)
from knowledge_workbench.worker.job_runner import (
    mark_transient_failure_sync as mark_job_transient_failure_sync,
)


class SourceExtractionWorker(JobRunner):
    def __init__(self, settings: Settings, gateway: AIGateway) -> None:
        super().__init__(settings, JobKind.SOURCE_EXTRACT)
        self._gateway = gateway

    async def _run_claimed(
        self, session: AsyncSession, *, job_id: UUID, token: ClaimToken
    ) -> None:
        prepared = await self._prepare(session, job_id=job_id, token=token)
        if prepared is None:
            return
        extraction, source, sections = prepared
        try:
            batches = build_extraction_batches(
                sections, max_characters=self._settings.extraction_batch_max_characters
            )
        except ValueError as exc:
            raise PermanentJobError("extraction_section_too_large", str(exc)) from exc
        if not batches:
            raise PermanentJobError(
                "extraction_source_empty", "The parsed source has no sections to extract."
            )

        generated: list[CandidateDraft] = []
        results: list[StructuredGenerationResult[ExtractionBatchOutput]] = []
        for batch in batches:
            try:
                result = await self._gateway.generate_structured(
                    StructuredGenerationRequest(
                        operation="extract_knowledge_candidates",
                        system_instruction=extraction_system_prompt(),
                        messages=[
                            AIMessage(
                                role="user",
                                content=extraction_user_message(source, batch),
                            )
                        ],
                        response_model=ExtractionBatchOutput,
                        metadata={
                            "source_version_id": str(extraction.source_version_id),
                            "prompt_version": extraction.prompt_version,
                        },
                    )
                )
            except (AITimeoutError, AIRateLimitedError, AIUnavailableError):
                raise
            except AIRefusalError as exc:
                raise PermanentJobError("ai_refusal", str(exc)) from exc
            except AIInvalidOutputError as exc:
                raise PermanentJobError("ai_invalid_output", str(exc)) from exc
            except AIGatewayError as exc:
                raise PermanentJobError("ai_request_rejected", str(exc)) from exc
            results.append(result)
            generated.extend(result.value.candidates)

        allowed = {section.id: section for section in sections}
        unique: list[CandidateDraft] = []
        seen: set[str] = set()
        for candidate in generated:
            invalid = set(candidate.evidence_section_ids) - allowed.keys()
            if invalid:
                raise PermanentJobError(
                    "invalid_evidence_reference",
                    "The model returned evidence outside the current source version.",
                )
            fingerprint = candidate_fingerprint(candidate)
            if fingerprint not in seen:
                seen.add(fingerprint)
                unique.append(candidate)
        await self._finish(
            session,
            job_id=job_id,
            token=token,
            extraction=extraction,
            candidates=unique,
            sections=allowed,
            results=results,
        )

    async def _prepare(
        self, session: AsyncSession, *, job_id: UUID, token: ClaimToken
    ) -> tuple[ExtractionJob, Source, list[SourceSection]] | None:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not self._owns(job, token):
                return None
            assert job is not None
            extraction = await session.scalar(
                select(ExtractionJob)
                .where(ExtractionJob.job_id == job_id)
                .with_for_update()
            )
            if extraction is None:
                raise PermanentJobError(
                    "extraction_job_missing", "The extraction record is unavailable."
                )
            version = await session.get(SourceVersion, extraction.source_version_id)
            if version is None or version.current_parse_artifact_id != extraction.parse_artifact_id:
                raise PermanentJobError(
                    "extraction_parse_revision_changed",
                    "The extraction request no longer points to the current parse revision.",
                )
            source = await session.scalar(
                select(Source).where(
                    Source.id == version.source_id, Source.space_id == job.space_id
                )
            )
            if source is None:
                raise PermanentJobError("source_not_found", "The source was not found.")
            sections = list(
                await session.scalars(
                    select(SourceSection)
                    .where(SourceSection.parse_artifact_id == extraction.parse_artifact_id)
                    .order_by(SourceSection.ordinal)
                )
            )
            extraction.status = ExtractionStatus.RUNNING.value
            extraction.started_at = extraction.started_at or datetime.now(UTC)
            extraction.error_code = None
            extraction.error_message = None
            job.progress = 20
            await session.flush()
            return extraction, source, sections

    async def _finish(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        token: ClaimToken,
        extraction: ExtractionJob,
        candidates: list[CandidateDraft],
        sections: dict[UUID, SourceSection],
        results: list[StructuredGenerationResult[ExtractionBatchOutput]],
    ) -> bool:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not self._owns(job, token):
                return False
            assert job is not None
            attempt = await session.scalar(
                select(JobAttempt)
                .where(
                    JobAttempt.id == token.attempt_id,
                    JobAttempt.job_id == job.id,
                    JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    JobAttempt.lease_expires_at > datetime.now(UTC),
                )
                .with_for_update()
            )
            db_extraction = await session.scalar(
                select(ExtractionJob)
                .where(ExtractionJob.id == extraction.id)
                .with_for_update()
            )
            if attempt is None or db_extraction is None:
                return False
            existing_candidates = list(
                await session.scalars(
                    select(ExtractionCandidate)
                    .where(ExtractionCandidate.extraction_job_id == db_extraction.id)
                    .with_for_update()
                )
            )
            if any(_candidate_was_reviewed(candidate) for candidate in existing_candidates):
                raise PermanentJobError(
                    "extraction_candidates_reviewed",
                    "Reviewed candidates cannot be replaced by an extraction retry.",
                )
            await session.execute(
                delete(ExtractionCandidate).where(
                    ExtractionCandidate.extraction_job_id == db_extraction.id,
                    ExtractionCandidate.version == 1,
                )
            )
            for ordinal, draft in enumerate(candidates):
                candidate = ExtractionCandidate(
                    id=uuid4(),
                    extraction_job_id=db_extraction.id,
                    space_id=db_extraction.space_id,
                    source_version_id=db_extraction.source_version_id,
                    ordinal=ordinal,
                    title=draft.title.strip(),
                    body=draft.body.strip(),
                    tags=list(dict.fromkeys(tag.strip() for tag in draft.tags if tag.strip())),
                    suggested_destination_id=None,
                    atomicity=draft.atomicity.value,
                    confidence=draft.confidence,
                    status=(
                        CandidateStatus.NEEDS_VERIFICATION.value
                        if draft.needs_verification
                        else CandidateStatus.PENDING_REVIEW.value
                    ),
                    verification_reason=draft.verification_reason,
                    conditions=draft.conditions,
                    exceptions=draft.exceptions,
                )
                session.add(candidate)
                for section_id in dict.fromkeys(draft.evidence_section_ids):
                    section = sections[section_id]
                    session.add(
                        CandidateEvidence(
                            id=uuid4(),
                            candidate_id=candidate.id,
                            source_version_id=db_extraction.source_version_id,
                            section_id=section.id,
                            quote_hash=section.quote_hash,
                        )
                    )
            now = datetime.now(UTC)
            last = results[-1]
            db_extraction.status = ExtractionStatus.READY.value
            db_extraction.provider = last.provider
            db_extraction.model = last.model
            db_extraction.input_tokens = sum(result.usage.input_tokens for result in results)
            db_extraction.output_tokens = sum(result.usage.output_tokens for result in results)
            db_extraction.latency_ms = sum(result.latency_ms for result in results)
            db_extraction.provider_request_id = last.provider_request_id
            db_extraction.completed_at = now
            job.status = JobStatus.SUCCEEDED.value
            job.progress = 100
            job.retryable = False
            job.error_code = None
            job.error_message = None
            job.finished_at = now
            job.updated_at = now
            attempt.status = JobAttemptStatus.SUCCEEDED.value
            attempt.finished_at = now
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now
            await session.flush()
            return True

    async def _on_failed(self, session: AsyncSession, *, job: Job, now: datetime) -> None:
        extraction = await session.scalar(
            select(ExtractionJob).where(ExtractionJob.job_id == job.id)
        )
        if extraction is not None:
            extraction.status = ExtractionStatus.FAILED.value
            extraction.error_code = job.error_code
            extraction.error_message = job.error_message
            extraction.completed_at = now

    @staticmethod
    def _owns(job: Job | None, token: ClaimToken) -> bool:
        return bool(
            job is not None
            and job.kind == JobKind.SOURCE_EXTRACT.value
            and job.status == JobStatus.RUNNING.value
            and job.attempt_count == token.attempt_number
        )


def _candidate_was_reviewed(candidate: ExtractionCandidate) -> bool:
    return (
        candidate.version > 1
        or candidate.reviewed_at is not None
        or candidate.status
        in {
            CandidateStatus.ACCEPTED.value,
            CandidateStatus.REJECTED.value,
        }
    )


def _fake_extraction_response(request: StructuredGenerationRequest[Any]) -> dict[str, Any]:
    if not request.messages:
        return {"candidates": []}
    payload = json.loads(request.messages[0].content)
    evidence = payload.get("evidence_sections", [])
    candidates = []
    for section in evidence:
        text = str(section.get("text", "")).strip()
        if not text:
            continue
        heading_path = section.get("heading_path") or []
        title = str(heading_path[-1] if heading_path else "来源知识候选")
        candidates.append(
            {
                "title": title[:500],
                "body": text[:5000],
                "tags": [],
                "evidence_section_ids": [section["section_id"]],
                "suggested_destination_id": None,
                "atomicity": "atomic",
                "confidence": 0.75,
                "needs_verification": False,
                "verification_reason": None,
                "conditions": [],
                "exceptions": [],
            }
        )
    return {"candidates": candidates}


def create_extraction_gateway(settings: Settings) -> AIGateway:
    if settings.ai_provider == "anthropic":
        return ClaudeAIGateway(
            model=settings.claude_model,
            timeout_seconds=settings.ai_timeout_seconds,
            max_tokens=settings.extraction_max_output_tokens,
        )
    return FakeAIGateway({"extract_knowledge_candidates": _fake_extraction_response})


async def run_source_extract(
    settings: Settings,
    *,
    job_id: UUID,
    celery_task_id: str | None,
    worker_name: str | None,
) -> None:
    engine = create_engine(settings)
    sessions: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    try:
        async with sessions() as session:
            await SourceExtractionWorker(settings, create_extraction_gateway(settings)).run(
                session,
                job_id=job_id,
                celery_task_id=celery_task_id,
                worker_name=worker_name,
            )
    finally:
        await engine.dispose()


def run_source_extract_sync(
    settings: Settings,
    *,
    job_id: UUID,
    celery_task_id: str | None,
    worker_name: str | None,
) -> None:
    asyncio.run(
        run_source_extract(
            settings,
            job_id=job_id,
            celery_task_id=celery_task_id,
            worker_name=worker_name,
        )
    )


async def mark_transient_failure(
    settings: Settings, *, job_id: UUID, token: ClaimToken, message: str
) -> None:
    await mark_job_transient_failure(
        settings,
        job_kind=JobKind.SOURCE_EXTRACT,
        job_id=job_id,
        token=token,
        message=message,
        error_code="source_extract_transient_failure",
    )


def mark_transient_failure_sync(
    settings: Settings, *, job_id: UUID, token: ClaimToken, message: str
) -> None:
    mark_job_transient_failure_sync(
        settings,
        job_kind=JobKind.SOURCE_EXTRACT,
        job_id=job_id,
        token=token,
        message=message,
        error_code="source_extract_transient_failure",
    )
