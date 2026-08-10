from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from knowledge_workbench.application.candidate_review import (
    CandidateAccept,
    CandidateNeedsVerification,
    CandidateReject,
    CandidateReviewService,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    ActivityEvent,
    CandidateAtomicity,
    CandidateEvidence,
    CandidateReview,
    CandidateReviewRequest,
    CandidateReviewResult,
    CandidateStatus,
    ExtractionCandidate,
    ExtractionJob,
    Job,
    JobKind,
    JobStatus,
    KnowledgeNode,
    OutboxEvent,
    ReviewCard,
    Source,
    SourceKind,
    SourceParseArtifact,
    SourceSection,
    SourceStatus,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL; set RUN_POSTGRES_INTEGRATION=1",
)

DEFAULT_SPACE_ID = UUID("01982ba0-4f20-7000-8000-000000000001")
DEFAULT_DESTINATION_ID = UUID("01982ba0-4f20-7000-8000-000000000002")


@pytest.fixture
async def postgres_session() -> AsyncIterator[AsyncSession]:
    engine: AsyncEngine = create_engine(Settings(app_env="test"))
    try:
        connection = await engine.connect()
    except (OSError, SQLAlchemyError) as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL is unavailable: {exc}")

    try:
        transaction = await connection.begin()
        candidate_table = await connection.scalar(
            text("SELECT to_regclass('public.candidate_review_results')")
        )
        if candidate_table is None:
            await transaction.rollback()
            pytest.skip("PostgreSQL is not migrated through D5")

        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()
    finally:
        await connection.close()
        await engine.dispose()


@dataclass(frozen=True)
class CandidateFixture:
    candidate: ExtractionCandidate
    evidence: CandidateEvidence
    section: SourceSection


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _seed_candidate(
    session: AsyncSession, *, title: str = "Candidate title"
) -> CandidateFixture:
    source = Source(
        id=uuid4(),
        space_id=DEFAULT_SPACE_ID,
        kind=SourceKind.PASTED_TEXT.value,
        title=f"Source {uuid4()}",
        status=SourceStatus.ACTIVE.value,
    )
    session.add(source)
    await session.flush()

    version = SourceVersion(
        id=uuid4(),
        source_id=source.id,
        version_number=1,
        content_sha256=_sha256("source content"),
        storage_key=f"integration/{uuid4()}",
        object_etag="etag",
        acquisition_type="pasted_text",
        acquisition_metadata={},
        processing_status="ready",
        parse_status="ready",
    )
    session.add(version)
    await session.flush()

    artifact = SourceParseArtifact(
        id=uuid4(),
        source_version_id=version.id,
        revision=1,
        parser_name="integration",
        parser_version="1",
        parser_config={},
        status="ready",
        canonical_content_sha256=_sha256("canonical content"),
        warnings=[],
        artifact_metadata={},
    )
    session.add(artifact)
    await session.flush()
    version.current_parse_artifact_id = artifact.id

    job = Job(
        id=uuid4(),
        space_id=DEFAULT_SPACE_ID,
        source_version_id=version.id,
        kind=JobKind.SOURCE_EXTRACT.value,
        status=JobStatus.SUCCEEDED.value,
        progress=100,
        attempt_count=1,
        attempt_budget_start=0,
        retryable=False,
    )
    session.add(job)
    await session.flush()

    extraction = ExtractionJob(
        id=uuid4(),
        job_id=job.id,
        space_id=DEFAULT_SPACE_ID,
        source_version_id=version.id,
        parse_artifact_id=artifact.id,
        status="ready",
        provider="fake",
        model="integration",
        prompt_version=f"integration-{uuid4()}",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
    )
    session.add(extraction)
    await session.flush()

    section_id = uuid4()
    quote_hash = _sha256("Evidence quote")
    section = SourceSection(
        id=section_id,
        parse_artifact_id=artifact.id,
        source_version_id=version.id,
        space_id=DEFAULT_SPACE_ID,
        block_id="block-1",
        ordinal=0,
        block_type="paragraph",
        text="Evidence quote",
        heading_path=[],
        locator={
            "sourceVersionId": str(version.id),
            "parseArtifactId": str(artifact.id),
            "sectionId": str(section_id),
            "quoteHash": quote_hash,
        },
        quote_hash=quote_hash,
        content_hash=_sha256("canonical content"),
        provenance={},
    )
    candidate = ExtractionCandidate(
        id=uuid4(),
        extraction_job_id=extraction.id,
        space_id=DEFAULT_SPACE_ID,
        source_version_id=version.id,
        ordinal=0,
        title=title,
        body="Candidate body",
        tags=["tag"],
        suggested_destination_id=DEFAULT_DESTINATION_ID,
        atomicity=CandidateAtomicity.ATOMIC.value,
        confidence=0.9,
        status=CandidateStatus.PENDING_REVIEW.value,
        version=1,
        conditions=[],
        exceptions=[],
    )
    evidence = CandidateEvidence(
        id=uuid4(),
        candidate_id=candidate.id,
        source_version_id=version.id,
        section_id=section.id,
        quote_hash=section.quote_hash,
    )
    session.add_all([section, candidate, evidence])
    await session.flush()
    return CandidateFixture(candidate, evidence, section)


def _snapshot_payload(candidate: ExtractionCandidate) -> dict[str, object]:
    return {
        "expected_version": candidate.version,
        "title": candidate.title,
        "body": candidate.body,
        "tags": candidate.tags,
        "suggested_destination_id": candidate.suggested_destination_id,
        "conditions": candidate.conditions,
        "exceptions": candidate.exceptions,
    }


async def test_accept_is_exactly_once_and_stale_versions_conflict(
    postgres_session: AsyncSession,
) -> None:
    seeded = await _seed_candidate(postgres_session)
    candidate = seeded.candidate
    request = CandidateAccept.model_validate(_snapshot_payload(candidate))
    service = CandidateReviewService()

    first = await service.accept(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        candidate_id=candidate.id,
        idempotency_key="accept-once",
        request=request,
    )
    replay = await service.accept(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        candidate_id=candidate.id,
        idempotency_key="accept-once",
        request=request,
    )

    assert replay == first
    assert first.candidate_status == CandidateStatus.ACCEPTED.value
    assert first.candidate_version == 2
    assert len(first.evidence_ids) == 1
    assert first.knowledge_node_id is not None
    assert first.knowledge_revision_id is not None
    assert first.review_card_id is not None
    assert candidate.reviewed_at is not None
    review = await postgres_session.scalar(
        select(CandidateReview).where(CandidateReview.candidate_id == candidate.id)
    )
    assert review is not None
    assert review.actor == "local"
    assert review.before_snapshot["title"] == "Candidate title"
    assert review.after_snapshot["status"] == CandidateStatus.ACCEPTED.value
    assert review.after_snapshot["version"] == 2
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(KnowledgeNode)
            .where(KnowledgeNode.origin_candidate_id == candidate.id)
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(CandidateReviewRequest)
            .where(CandidateReviewRequest.candidate_id == candidate.id)
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(CandidateReview)
            .where(CandidateReview.candidate_id == candidate.id)
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(CandidateReviewResult)
            .join(CandidateReviewRequest)
            .where(CandidateReviewRequest.candidate_id == candidate.id)
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ActivityEvent)
            .where(ActivityEvent.entity_id == candidate.id)
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == candidate.id)
        )
        == 1
    )
    assert await postgres_session.scalar(select(func.count()).select_from(ReviewCard)) == 1

    changed_request = CandidateAccept.model_validate(
        {**_snapshot_payload(candidate), "expected_version": 1, "body": "Changed body"}
    )
    with pytest.raises(AppError) as caught:
        await service.accept(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            candidate_id=candidate.id,
            idempotency_key="accept-once",
            request=changed_request,
        )
    assert caught.value.code == "idempotency_conflict"

    with pytest.raises(AppError) as caught:
        await service.accept(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            candidate_id=candidate.id,
            idempotency_key="accept-stale",
            request=request,
        )
    assert caught.value.code == "candidate_version_conflict"


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [("needs_verification", CandidateStatus.NEEDS_VERIFICATION.value), ("reject", "rejected")],
)
async def test_non_acceptance_reviews_create_no_knowledge_or_card(
    postgres_session: AsyncSession, action: str, expected_status: str
) -> None:
    seeded = await _seed_candidate(postgres_session, title=action)
    candidate = seeded.candidate
    service = CandidateReviewService()
    payload = _snapshot_payload(candidate)

    if action == "needs_verification":
        outcome = await service.mark_needs_verification(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            candidate_id=candidate.id,
            idempotency_key=f"{action}-once",
            request=CandidateNeedsVerification.model_validate(
                {**payload, "reason": "  source needs checking  "}
            ),
        )
        assert candidate.verification_reason == "source needs checking"
    else:
        outcome = await service.reject(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            candidate_id=candidate.id,
            idempotency_key=f"{action}-once",
            request=CandidateReject.model_validate({**payload, "reason": "  duplicate  "}),
        )
        assert candidate.rejection_reason == "duplicate"

    assert outcome.candidate_status == expected_status
    assert candidate.reviewed_at is not None
    assert outcome.knowledge_node_id is None
    assert outcome.knowledge_revision_id is None
    assert outcome.review_card_id is None
    assert outcome.evidence_ids == []
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(KnowledgeNode)
            .where(KnowledgeNode.origin_candidate_id == candidate.id)
        )
        == 0
    )
    result = await postgres_session.scalar(
        select(CandidateReviewResult)
        .join(CandidateReviewRequest)
        .where(CandidateReviewRequest.candidate_id == candidate.id)
    )
    assert result is not None
    assert result.knowledge_node_id is None
    assert result.review_card_id is None


async def test_accept_rejects_evidence_hash_mismatch_without_writes(
    postgres_session: AsyncSession,
) -> None:
    seeded = await _seed_candidate(postgres_session)
    candidate = seeded.candidate
    seeded.evidence.quote_hash = "f" * 64
    await postgres_session.flush()

    with pytest.raises(AppError) as caught:
        await CandidateReviewService().accept(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            candidate_id=candidate.id,
            idempotency_key="mismatched-evidence",
            request=CandidateAccept.model_validate(_snapshot_payload(candidate)),
        )

    assert caught.value.code == "candidate_evidence_stale"
    assert candidate.status == CandidateStatus.PENDING_REVIEW.value
    assert candidate.version == 1
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(CandidateReviewRequest)
            .where(CandidateReviewRequest.candidate_id == candidate.id)
        )
        == 0
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(CandidateReview)
            .where(CandidateReview.candidate_id == candidate.id)
        )
        == 0
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ActivityEvent)
            .where(ActivityEvent.entity_id == candidate.id)
        )
        == 0
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == candidate.id)
        )
        == 0
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(KnowledgeNode)
            .where(KnowledgeNode.origin_candidate_id == candidate.id)
        )
        == 0
    )


async def test_failed_accept_can_be_rolled_back_without_partial_rows(
    postgres_session: AsyncSession,
) -> None:
    seeded = await _seed_candidate(postgres_session)
    candidate_id = seeded.candidate.id
    savepoint = await postgres_session.begin_nested()

    outcome = await CandidateReviewService().accept(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        candidate_id=candidate_id,
        idempotency_key="rollback-accept",
        request=CandidateAccept.model_validate(_snapshot_payload(seeded.candidate)),
    )
    assert outcome.knowledge_node_id is not None

    await savepoint.rollback()
    postgres_session.expire_all()

    candidate = await postgres_session.get(ExtractionCandidate, candidate_id)
    assert candidate is not None
    assert candidate.status == CandidateStatus.PENDING_REVIEW.value
    assert candidate.version == 1
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(CandidateReviewRequest)
            .where(CandidateReviewRequest.candidate_id == candidate_id)
        )
        == 0
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(KnowledgeNode)
            .where(KnowledgeNode.origin_candidate_id == candidate_id)
        )
        == 0
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ActivityEvent)
            .where(ActivityEvent.entity_id == candidate_id)
        )
        == 0
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == candidate_id)
        )
        == 0
    )
