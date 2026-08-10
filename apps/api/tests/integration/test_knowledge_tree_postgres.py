from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from knowledge_workbench.application.knowledge_tree import (
    KnowledgeNodeCreate,
    KnowledgeNodeDelete,
    KnowledgeNodeEdit,
    KnowledgeNodeMove,
    KnowledgeTreeService,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    KnowledgeEvidence,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeRevision,
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
    reason="requires PostgreSQL migrated through D6; set RUN_POSTGRES_INTEGRATION=1",
)

DEFAULT_SPACE_ID = UUID("01982ba0-4f20-7000-8000-000000000001")


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
        d6_table = await connection.scalar(
            text("SELECT to_regclass('public.knowledge_write_requests')")
        )
        if d6_table is None:
            await transaction.rollback()
            pytest.skip("PostgreSQL is not migrated through D6")

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


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _root(session: AsyncSession) -> KnowledgeNode:
    root = await session.scalar(
        select(KnowledgeNode).where(
            KnowledgeNode.space_id == DEFAULT_SPACE_ID,
            KnowledgeNode.kind == KnowledgeNodeKind.ROOT.value,
            KnowledgeNode.deleted_at.is_(None),
        )
    )
    assert root is not None
    return root


async def test_tree_writes_replay_move_search_and_soft_delete(
    postgres_session: AsyncSession,
) -> None:
    service = KnowledgeTreeService()
    root = await _root(postgres_session)

    folder_request = KnowledgeNodeCreate(
        parent_id=root.id,
        kind=KnowledgeNodeKind.FOLDER,
        expected_version=root.version,
        title="Research folder",
    )
    folder_outcome = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key=f"folder-{uuid4()}",
        request=folder_request,
    )
    folder = await postgres_session.get(KnowledgeNode, folder_outcome.node_id)
    assert folder is not None
    assert folder.path == f"{root.path}.n{folder.id.hex}"

    document_key = f"document-{uuid4()}"
    document_request = KnowledgeNodeCreate(
        parent_id=root.id,
        kind=KnowledgeNodeKind.DOCUMENT,
        expected_version=root.version,
        title="Durable document",
        body="Initial body",
    )
    document_outcome = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key=document_key,
        request=document_request,
    )
    replay = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key=document_key,
        request=document_request,
    )
    assert replay == document_outcome

    document = await postgres_session.get(KnowledgeNode, document_outcome.node_id)
    assert document is not None
    first_revision_id = document.current_revision_id
    assert first_revision_id is not None
    edit_request = KnowledgeNodeEdit(
        expected_version=document.version,
        expected_revision_id=first_revision_id,
        title="Durable document revised",
        body="Searchable integration body",
        reason="Integration verification",
    )
    edit_key = f"edit-{uuid4()}"
    edited = await service.edit(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        node_id=document.id,
        idempotency_key=edit_key,
        request=edit_request,
    )
    assert edited.node_version == 2
    assert document.current_revision_id != first_revision_id
    assert (
        await service.edit(
            postgres_session,
            space_id=DEFAULT_SPACE_ID,
            node_id=document.id,
            idempotency_key=edit_key,
            request=edit_request,
        )
        == edited
    )

    moved = await service.move(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        node_id=document.id,
        idempotency_key=f"move-{uuid4()}",
        request=KnowledgeNodeMove(
            expected_version=document.version,
            parent_id=folder.id,
        ),
    )
    assert moved.node_version == 3
    assert document.parent_id == folder.id
    assert document.path == f"{folder.path}.n{document.id.hex}"

    search_results = await service.search(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        query="integration body",
    )
    result = next(item for item in search_results if item.record.node.id == document.id)
    assert "body" in result.match_fields
    assert result.ancestor_ids == [root.id, folder.id]
    assert result.breadcrumb.endswith("Research folder / Durable document revised")

    revision_ids = list(
        await postgres_session.scalars(
            select(KnowledgeRevision.id).where(KnowledgeRevision.node_id == document.id)
        )
    )
    assert len(revision_ids) == 2
    await service.delete(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        node_id=folder.id,
        idempotency_key=f"delete-{uuid4()}",
        request=KnowledgeNodeDelete(expected_version=folder.version),
    )
    assert folder.deleted_at is not None
    assert document.deleted_at == folder.deleted_at
    assert (
        await postgres_session.scalar(
            select(KnowledgeRevision.id).where(KnowledgeRevision.id == first_revision_id)
        )
        == first_revision_id
    )


async def test_citation_and_source_search_remain_bound_to_historical_artifact(
    postgres_session: AsyncSession,
) -> None:
    service = KnowledgeTreeService()
    root = await _root(postgres_session)
    document_outcome = await service.create(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        idempotency_key=f"citation-document-{uuid4()}",
        request=KnowledgeNodeCreate(
            parent_id=root.id,
            kind=KnowledgeNodeKind.DOCUMENT,
            expected_version=root.version,
            title="Citation document",
        ),
    )
    document = await postgres_session.get(KnowledgeNode, document_outcome.node_id)
    assert document is not None
    revision = await postgres_session.get(KnowledgeRevision, document.current_revision_id)
    assert revision is not None

    source = Source(
        id=uuid4(),
        space_id=DEFAULT_SPACE_ID,
        kind=SourceKind.PASTED_TEXT.value,
        title=f"Historical source {uuid4()}",
        status=SourceStatus.ACTIVE.value,
    )
    version = SourceVersion(
        id=uuid4(),
        source_id=source.id,
        version_number=1,
        content_sha256=_sha256("source-v1"),
        storage_key=f"integration/{uuid4()}",
        object_etag="etag",
        acquisition_type="pasted_text",
        acquisition_metadata={},
        processing_status="ready",
        parse_status="ready",
    )
    postgres_session.add_all([source, version])
    await postgres_session.flush()

    historical_artifact = SourceParseArtifact(
        id=uuid4(),
        source_version_id=version.id,
        revision=1,
        parser_name="integration",
        parser_version="1",
        parser_config={},
        status="ready",
        canonical_content_sha256=_sha256("historical-canonical"),
        warnings=[],
        artifact_metadata={},
    )
    current_artifact = SourceParseArtifact(
        id=uuid4(),
        source_version_id=version.id,
        revision=2,
        parser_name="integration",
        parser_version="2",
        parser_config={},
        status="ready",
        canonical_content_sha256=_sha256("current-canonical"),
        warnings=[],
        artifact_metadata={},
    )
    postgres_session.add_all([historical_artifact, current_artifact])
    await postgres_session.flush()
    version.current_parse_artifact_id = current_artifact.id

    quote = "Frozen historical quote"
    historical_section = SourceSection(
        id=uuid4(),
        parse_artifact_id=historical_artifact.id,
        source_version_id=version.id,
        space_id=DEFAULT_SPACE_ID,
        block_id="historical-block",
        ordinal=3,
        block_type="paragraph",
        text=quote,
        heading_path=["History"],
        locator={"page": 4},
        quote_hash=_sha256(quote),
        content_hash=_sha256("historical-canonical"),
        provenance={},
    )
    current_section = SourceSection(
        id=uuid4(),
        parse_artifact_id=current_artifact.id,
        source_version_id=version.id,
        space_id=DEFAULT_SPACE_ID,
        block_id="current-block",
        ordinal=0,
        block_type="paragraph",
        text="Replacement text",
        heading_path=[],
        locator={"page": 1},
        quote_hash=_sha256("Replacement text"),
        content_hash=_sha256("current-canonical"),
        provenance={},
    )
    evidence = KnowledgeEvidence(
        id=uuid4(),
        revision_id=revision.id,
        space_id=DEFAULT_SPACE_ID,
        source_version_id=version.id,
        parse_artifact_id=historical_artifact.id,
        section_id=historical_section.id,
        quote_hash=historical_section.quote_hash,
        content_hash=historical_section.content_hash,
        locator=dict(historical_section.locator),
        frozen_quote=quote,
    )
    postgres_session.add_all([historical_section, current_section, evidence])
    await postgres_session.flush()

    record = await service.get_evidence(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        evidence_id=evidence.id,
    )
    assert record.evidence.frozen_quote == quote
    assert record.evidence.parse_artifact_id == historical_artifact.id
    assert record.artifact_revision == 1
    assert record.section_ordinal == 3
    assert record.source_id == source.id

    detail = await service.get_node(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        node_id=document.id,
    )
    assert [item.evidence.id for item in detail.evidence] == [evidence.id]
    search_results = await service.search(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        query=source.title,
    )
    source_match = next(
        item for item in search_results if item.record.node.id == document.id
    )
    assert source_match.match_fields == ["source_title"]
