from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from knowledge_workbench.application.knowledge_update_proposal import (
    KnowledgeUpdateProposalService,
    ProposalCreate,
    ProposalDecision,
    ProposalSubmit,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    ActivityEvent,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeRevision,
    KnowledgeSpace,
    KnowledgeUpdateProposal,
    KnowledgeUpdateProposalEvidence,
    KnowledgeUpdateProposalRequest,
    KnowledgeUpdateProposalTransition,
    OutboxEvent,
    ParseArtifactStatus,
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


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
        table = await connection.scalar(
            text("SELECT to_regclass('public.knowledge_update_proposals')")
        )
        if table is None:
            await transaction.rollback()
            pytest.skip("PostgreSQL is not migrated through N2.1")
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


async def _seed_ready_section(
    session: AsyncSession, *, space_id: UUID = DEFAULT_SPACE_ID
) -> SourceSection:
    source = Source(
        id=uuid4(),
        space_id=space_id,
        kind=SourceKind.PASTED_TEXT.value,
        title=f"Proposal source {uuid4()}",
        status=SourceStatus.ACTIVE.value,
    )
    session.add(source)
    await session.flush()
    version = SourceVersion(
        id=uuid4(),
        source_id=source.id,
        version_number=1,
        content_sha256=_sha256("proposal content"),
        storage_key=f"proposal/{uuid4()}",
        object_etag="proposal-etag",
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
        status=ParseArtifactStatus.READY.value,
        canonical_content_sha256=_sha256("proposal canonical"),
        warnings=[],
        artifact_metadata={},
    )
    session.add(artifact)
    await session.flush()
    version.current_parse_artifact_id = artifact.id
    section_id = uuid4()
    quote_hash = _sha256("proposal quote")
    section = SourceSection(
        id=section_id,
        parse_artifact_id=artifact.id,
        source_version_id=version.id,
        space_id=space_id,
        block_id=f"block-{uuid4()}",
        ordinal=0,
        block_type="paragraph",
        text="proposal quote",
        heading_path=[],
        locator={
            "sourceVersionId": str(version.id),
            "parseArtifactId": str(artifact.id),
            "sectionId": str(section_id),
            "quoteHash": quote_hash,
        },
        quote_hash=quote_hash,
        content_hash=_sha256("proposal canonical"),
        provenance={},
    )
    session.add(section)
    await session.flush()
    return section


def _create_request(section: SourceSection) -> ProposalCreate:
    return ProposalCreate.model_validate(
        {
            "action": "create",
            "suggested_title": "Proposed title",
            "suggested_body": "Proposed body",
            "evidence": [{"role": "new_support", "section_id": str(section.id)}],
        }
    )


async def _seed_target_node(
    session: AsyncSession,
) -> tuple[KnowledgeNode, KnowledgeRevision]:
    root = await session.scalar(
        select(KnowledgeNode).where(
            KnowledgeNode.space_id == DEFAULT_SPACE_ID,
            KnowledgeNode.kind == KnowledgeNodeKind.ROOT.value,
            KnowledgeNode.deleted_at.is_(None),
        )
    )
    assert root is not None
    node = KnowledgeNode(
        id=uuid4(),
        space_id=DEFAULT_SPACE_ID,
        parent_id=root.id,
        kind=KnowledgeNodeKind.DOCUMENT.value,
        path=f"{root.path}.n{uuid4().hex}",
        version=1,
        sort_order=0,
    )
    session.add(node)
    await session.flush()
    revision = KnowledgeRevision(
        id=uuid4(),
        node_id=node.id,
        space_id=DEFAULT_SPACE_ID,
        revision_number=1,
        title="Proposal target",
        body="Current revision",
        tags=[],
        conditions=[],
        exceptions=[],
        actor="integration",
        content_hash=_sha256("proposal target revision"),
    )
    session.add(revision)
    await session.flush()
    node.current_revision_id = revision.id
    await session.flush()
    return node, revision


async def test_create_submit_approve_is_exactly_once_and_does_not_write_knowledge(
    postgres_session: AsyncSession,
) -> None:
    section = await _seed_ready_section(postgres_session)
    service = KnowledgeUpdateProposalService()
    before_revisions = await postgres_session.scalar(
        select(func.count()).select_from(KnowledgeRevision)
    )

    created = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key="proposal-create-once",
        request=_create_request(section),
    )
    replay = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key="proposal-create-once",
        request=_create_request(section),
    )
    assert replay == created

    submitted = await service.submit(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        proposal_id=created.proposal_id,
        idempotency_key="proposal-submit-once",
        request=ProposalSubmit(expected_version=1),
    )
    approved = await service.approve(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        proposal_id=created.proposal_id,
        idempotency_key="proposal-approve-once",
        request=ProposalDecision(expected_version=submitted.proposal_version),
    )
    assert approved.proposal_status == "approved"
    assert approved.proposal_version == 3
    assert (
        await postgres_session.scalar(select(func.count()).select_from(KnowledgeRevision))
        == before_revisions
    )
    assert await postgres_session.scalar(
        select(func.count()).select_from(KnowledgeUpdateProposalRequest).where(
            KnowledgeUpdateProposalRequest.proposal_id == created.proposal_id
        )
    ) == 3
    assert await postgres_session.scalar(
        select(func.count()).select_from(KnowledgeUpdateProposalTransition).where(
            KnowledgeUpdateProposalTransition.proposal_id == created.proposal_id
        )
    ) == 3
    assert await postgres_session.scalar(
        select(func.count())
        .select_from(ActivityEvent)
        .where(ActivityEvent.entity_id == created.proposal_id)
    ) == 3
    assert await postgres_session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.aggregate_id == created.proposal_id)
    ) == 3


async def test_same_idempotency_key_cannot_replay_another_proposal(
    postgres_session: AsyncSession,
) -> None:
    first_section = await _seed_ready_section(postgres_session)
    second_section = await _seed_ready_section(postgres_session)
    service = KnowledgeUpdateProposalService()
    first = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key="proposal-same-key-first",
        request=_create_request(first_section),
    )
    second = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key="proposal-same-key-second",
        request=_create_request(second_section),
    )
    await service.submit(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        proposal_id=first.proposal_id,
        idempotency_key="proposal-shared-submit",
        request=ProposalSubmit(expected_version=1),
    )

    with pytest.raises(AppError) as caught:
        await service.submit(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            proposal_id=second.proposal_id,
            idempotency_key="proposal-shared-submit",
            request=ProposalSubmit(expected_version=1),
        )
    assert caught.value.code == "idempotency_conflict"
    second_row = await postgres_session.get(KnowledgeUpdateProposal, second.proposal_id)
    assert second_row is not None
    assert second_row.status == "draft"
    assert second_row.version == 1


async def test_stale_evidence_fails_approval_without_audit_write(
    postgres_session: AsyncSession,
) -> None:
    section = await _seed_ready_section(postgres_session)
    service = KnowledgeUpdateProposalService()
    created = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key="proposal-stale-create",
        request=_create_request(section),
    )
    await service.submit(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        proposal_id=created.proposal_id,
        idempotency_key="proposal-stale-submit",
        request=ProposalSubmit(expected_version=1),
    )
    section.text = "changed after freeze"
    await postgres_session.flush()

    with pytest.raises(AppError) as caught:
        await service.approve(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            proposal_id=created.proposal_id,
            idempotency_key="proposal-stale-approve",
            request=ProposalDecision(expected_version=2),
        )
    assert caught.value.code == "proposal_evidence_stale"
    proposal = await postgres_session.get(KnowledgeUpdateProposal, created.proposal_id)
    assert proposal is not None
    assert proposal.status == "pending_review"
    assert proposal.version == 2
    assert await postgres_session.scalar(
        select(func.count()).select_from(KnowledgeUpdateProposalTransition).where(
            KnowledgeUpdateProposalTransition.proposal_id == created.proposal_id
        )
    ) == 2


async def test_stale_target_version_fails_approval_without_audit_write(
    postgres_session: AsyncSession,
) -> None:
    section = await _seed_ready_section(postgres_session)
    node, revision = await _seed_target_node(postgres_session)
    service = KnowledgeUpdateProposalService()
    proposal = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key="proposal-stale-target-create",
        request=ProposalCreate.model_validate(
            {
                "action": "revise",
                "target_node_id": str(node.id),
                "target_revision_id": str(revision.id),
                "suggested_title": "Updated target",
                "evidence": [{"role": "new_support", "section_id": str(section.id)}],
            }
        ),
    )
    await service.submit(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        proposal_id=proposal.proposal_id,
        idempotency_key="proposal-stale-target-submit",
        request=ProposalSubmit(expected_version=1),
    )
    node.version = 2
    await postgres_session.flush()

    with pytest.raises(AppError) as caught:
        await service.approve(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            proposal_id=proposal.proposal_id,
            idempotency_key="proposal-stale-target-approve",
            request=ProposalDecision(expected_version=2),
        )
    assert caught.value.code == "proposal_target_stale"
    persisted = await postgres_session.get(KnowledgeUpdateProposal, proposal.proposal_id)
    assert persisted is not None
    assert persisted.status == "pending_review"
    assert persisted.version == 2
    assert await postgres_session.scalar(
        select(func.count()).select_from(KnowledgeUpdateProposalTransition).where(
            KnowledgeUpdateProposalTransition.proposal_id == proposal.proposal_id
        )
    ) == 2


async def test_non_current_target_revision_fails_approval_without_audit_write(
    postgres_session: AsyncSession,
) -> None:
    section = await _seed_ready_section(postgres_session)
    node, revision = await _seed_target_node(postgres_session)
    service = KnowledgeUpdateProposalService()
    proposal = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key="proposal-noncurrent-target-create",
        request=ProposalCreate.model_validate(
            {
                "action": "revise",
                "target_node_id": str(node.id),
                "target_revision_id": str(revision.id),
                "suggested_title": "Updated target",
                "evidence": [{"role": "new_support", "section_id": str(section.id)}],
            }
        ),
    )
    await service.submit(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        proposal_id=proposal.proposal_id,
        idempotency_key="proposal-noncurrent-target-submit",
        request=ProposalSubmit(expected_version=1),
    )
    replacement = KnowledgeRevision(
        id=uuid4(),
        node_id=node.id,
        space_id=DEFAULT_SPACE_ID,
        revision_number=2,
        title="Replacement revision",
        body="Replacement revision body",
        tags=[],
        conditions=[],
        exceptions=[],
        actor="integration",
        content_hash=_sha256("proposal replacement revision"),
    )
    postgres_session.add(replacement)
    await postgres_session.flush()
    node.current_revision_id = replacement.id
    await postgres_session.flush()

    with pytest.raises(AppError) as caught:
        await service.approve(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            proposal_id=proposal.proposal_id,
            idempotency_key="proposal-noncurrent-target-approve",
            request=ProposalDecision(expected_version=2),
        )
    assert caught.value.code == "proposal_target_stale"
    persisted = await postgres_session.get(KnowledgeUpdateProposal, proposal.proposal_id)
    assert persisted is not None
    assert persisted.status == "pending_review"
    assert persisted.version == 2
    assert await postgres_session.scalar(
        select(func.count()).select_from(KnowledgeUpdateProposalTransition).where(
            KnowledgeUpdateProposalTransition.proposal_id == proposal.proposal_id
        )
    ) == 2


async def test_database_rejects_cross_space_proposal_references(
    postgres_session: AsyncSession,
) -> None:
    local_section = await _seed_ready_section(postgres_session)
    local_node, local_revision = await _seed_target_node(postgres_session)
    _, same_space_revision = await _seed_target_node(postgres_session)
    other_space = KnowledgeSpace(id=uuid4(), slug=f"proposal-{uuid4()}", name="Other space")
    postgres_session.add(other_space)
    await postgres_session.flush()
    same_space_section = await _seed_ready_section(postgres_session)
    other_section = await _seed_ready_section(postgres_session, space_id=other_space.id)
    local_source_id = await postgres_session.scalar(
        select(Source.id)
        .join(SourceVersion, SourceVersion.source_id == Source.id)
        .where(SourceVersion.id == local_section.source_version_id)
    )
    same_space_source_id = await postgres_session.scalar(
        select(Source.id)
        .join(SourceVersion, SourceVersion.source_id == Source.id)
        .where(SourceVersion.id == same_space_section.source_version_id)
    )
    other_source_id = await postgres_session.scalar(
        select(Source.id)
        .join(SourceVersion, SourceVersion.source_id == Source.id)
        .where(SourceVersion.id == other_section.source_version_id)
    )
    assert local_source_id is not None
    assert same_space_source_id is not None
    assert other_source_id is not None
    other_node = KnowledgeNode(
        id=uuid4(),
        space_id=other_space.id,
        parent_id=None,
        kind=KnowledgeNodeKind.ROOT.value,
        path=f"n{uuid4().hex}",
        version=1,
        sort_order=0,
    )
    postgres_session.add(other_node)
    await postgres_session.flush()
    other_revision = KnowledgeRevision(
        id=uuid4(),
        node_id=other_node.id,
        space_id=other_space.id,
        revision_number=1,
        title="Other revision",
        body="Other revision body",
        tags=[],
        conditions=[],
        exceptions=[],
        actor="integration",
        content_hash=_sha256("cross-space revision"),
    )
    postgres_session.add(other_revision)
    await postgres_session.flush()
    other_node.current_revision_id = other_revision.id
    await postgres_session.flush()
    proposal_values = {
        "id": uuid4(),
        "space_id": DEFAULT_SPACE_ID,
        "action": "revise",
        "status": "draft",
        "version": 1,
        "suggested_tags": [],
        "conditions": [],
        "exceptions": [],
    }

    async def assert_fk_rejected(values: dict[object, object]) -> None:
        savepoint = await postgres_session.begin_nested()
        try:
            with pytest.raises(IntegrityError):
                await postgres_session.execute(insert(KnowledgeUpdateProposal).values(**values))
                await postgres_session.flush()
        finally:
            await savepoint.rollback()

    await assert_fk_rejected(
        {
            **proposal_values,
            "target_node_id": local_node.id,
            "target_revision_id": same_space_revision.id,
        }
    )
    await assert_fk_rejected(
        {
            **proposal_values,
            "target_node_id": local_node.id,
            "target_revision_id": other_revision.id,
        }
    )
    valid_proposal = KnowledgeUpdateProposal(
        **proposal_values,
        target_node_id=local_node.id,
        target_revision_id=local_revision.id,
    )
    postgres_session.add(valid_proposal)
    await postgres_session.flush()
    evidence_values = {
        "id": uuid4(),
        "proposal_id": valid_proposal.id,
        "space_id": DEFAULT_SPACE_ID,
        "role": "new_support",
        "source_version_id": local_section.source_version_id,
        "parse_artifact_id": local_section.parse_artifact_id,
        "frozen_quote": local_section.text,
        "quote_hash": local_section.quote_hash,
        "content_hash": local_section.content_hash,
        "locator": local_section.locator,
        "ordinal": 0,
    }

    async def assert_evidence_fk_rejected(values: dict[object, object]) -> None:
        savepoint = await postgres_session.begin_nested()
        try:
            with pytest.raises(IntegrityError):
                await postgres_session.execute(
                    insert(KnowledgeUpdateProposalEvidence).values(**values)
                )
                await postgres_session.flush()
        finally:
            await savepoint.rollback()

    await assert_evidence_fk_rejected(
        {
            **evidence_values,
            "source_id": same_space_source_id,
            "section_id": same_space_section.id,
        }
    )
    await assert_evidence_fk_rejected(
        {
            **evidence_values,
            "source_id": other_source_id,
            "section_id": local_section.id,
        }
    )
    await assert_evidence_fk_rejected(
        {
            **evidence_values,
            "source_id": local_source_id,
            "section_id": other_section.id,
        }
    )
    await assert_evidence_fk_rejected(
        {
            **evidence_values,
            "source_id": local_source_id,
            "section_id": local_section.id,
            "knowledge_revision_id": other_revision.id,
        }
    )


async def test_nested_rollback_removes_proposal_audit_rows(
    postgres_session: AsyncSession,
) -> None:
    section = await _seed_ready_section(postgres_session)
    savepoint = await postgres_session.begin_nested()
    service = KnowledgeUpdateProposalService()
    created = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key="proposal-rollback",
        request=_create_request(section),
    )
    await savepoint.rollback()
    postgres_session.expire_all()
    assert await postgres_session.get(KnowledgeUpdateProposal, created.proposal_id) is None
    assert await postgres_session.scalar(
        select(func.count())
        .select_from(ActivityEvent)
        .where(ActivityEvent.entity_id == created.proposal_id)
    ) == 0
    assert await postgres_session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.aggregate_id == created.proposal_id)
    ) == 0
