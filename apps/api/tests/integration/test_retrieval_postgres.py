from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from knowledge_workbench.application.debug_retrieval import DebugRetrievalService
from knowledge_workbench.application.indexing import IndexingService
from knowledge_workbench.application.knowledge_tree import (
    KnowledgeNodeCreate,
    KnowledgeNodeDelete,
    KnowledgeNodeMove,
    KnowledgeTreeService,
)
from knowledge_workbench.application.retrieval import ScopeRequest, ScopeResolver
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobKind,
    JobStatus,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeRevision,
    KnowledgeSpace,
    RetrievalChunk,
    RetrievalIndexRun,
    Source,
    SourceKind,
    SourceParseArtifact,
    SourceSection,
    SourceStatus,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine
from knowledge_workbench.infrastructure.ai.fake_embedding import FakeEmbeddingGateway

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires PostgreSQL migrated through D7; set RUN_POSTGRES_INTEGRATION=1",
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
        d7_table = await connection.scalar(text("SELECT to_regclass('public.retrieval_chunks')"))
        if d7_table is None:
            await transaction.rollback()
            pytest.skip("PostgreSQL is not migrated through D7")

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


async def _create_node(
    session: AsyncSession,
    *,
    parent: KnowledgeNode,
    kind: KnowledgeNodeKind,
    title: str,
    body: str = "",
) -> tuple[KnowledgeNode, KnowledgeRevision]:
    outcome = await KnowledgeTreeService().create(
        session,
        space_id=parent.space_id,
        idempotency_key=f"retrieval-node-{uuid4()}",
        request=KnowledgeNodeCreate(
            parent_id=parent.id,
            kind=kind,
            expected_version=parent.version,
            title=title,
            body=body,
        ),
    )
    node = await session.get(KnowledgeNode, outcome.node_id)
    assert node is not None and node.current_revision_id is not None
    revision = await session.get(KnowledgeRevision, node.current_revision_id)
    assert revision is not None
    return node, revision


async def _add_knowledge_chunk(
    session: AsyncSession,
    *,
    settings: Settings,
    node: KnowledgeNode,
    revision: KnowledgeRevision,
    content: str,
    path: str | None = None,
) -> RetrievalChunk:
    job = Job(
        id=uuid4(),
        space_id=node.space_id,
        kind=JobKind.SOURCE_INDEX.value,
        status=JobStatus.SUCCEEDED.value,
        progress=100,
        attempt_count=1,
        attempt_budget_start=0,
        retryable=False,
        idempotency_key=f"retrieval-index-{uuid4()}",
    )
    run = RetrievalIndexRun(
        id=uuid4(),
        job_id=job.id,
        space_id=node.space_id,
        target_kind="knowledge_revision",
        target_id=revision.id,
        scope_node_id=node.id,
        input_hash=revision.content_hash,
        knowledge_revision_id=revision.id,
        status="ready",
        index_config_version=settings.index_version,
        chunker_version=settings.chunker_version,
        embedding_provider="fake",
        embedding_model=settings.embedding_model,
        embedding_config={"dimensions": settings.embedding_dimensions},
        embedding_dimensions=settings.embedding_dimensions,
        chunk_count=1,
        embedded_count=1,
    )
    vector = (
        await FakeEmbeddingGateway(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        ).embed([content])
    ).vectors[0]
    chunk = RetrievalChunk(
        id=uuid4(),
        index_run_id=run.id,
        space_id=node.space_id,
        corpus_kind="confirmed_knowledge",
        index_config_version=settings.index_version,
        active=True,
        knowledge_node_id=node.id,
        knowledge_revision_id=revision.id,
        ordinal=0,
        content_identity=_sha256(f"identity:{uuid4()}"),
        content_hash=_sha256(content),
        title=revision.title,
        text=content,
        char_count=len(content),
        token_count=max(1, len(content.split())),
        path=path or node.path,
        heading_path=[revision.title],
        embedding_model=settings.embedding_model,
        embedding_config={"provider": "fake", "dimensions": settings.embedding_dimensions},
        embedding=vector,
    )
    session.add_all([job, run])
    await session.flush()
    session.add(chunk)
    await session.flush()
    return chunk


async def _add_source_version(
    session: AsyncSession,
    *,
    settings: Settings,
    source: Source,
    version_number: int,
    path: str,
    content: str,
) -> RetrievalChunk:
    version = SourceVersion(
        id=uuid4(),
        source_id=source.id,
        version_number=version_number,
        content_sha256=_sha256(f"source:{source.id}:{version_number}"),
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
        canonical_content_sha256=_sha256(content),
        warnings=[],
        artifact_metadata={},
    )
    session.add(artifact)
    await session.flush()
    version.current_parse_artifact_id = artifact.id
    section = SourceSection(
        id=uuid4(),
        parse_artifact_id=artifact.id,
        source_version_id=version.id,
        space_id=source.space_id,
        block_id=f"block-{version_number}",
        ordinal=0,
        block_type="paragraph",
        text=content,
        heading_path=[source.title],
        locator={"page": 1},
        quote_hash=_sha256(content),
        content_hash=_sha256(content),
    )
    session.add(section)
    await session.flush()

    job = Job(
        id=uuid4(),
        space_id=source.space_id,
        source_version_id=version.id,
        kind=JobKind.SOURCE_INDEX.value,
        status=JobStatus.SUCCEEDED.value,
        progress=100,
        attempt_count=1,
        attempt_budget_start=0,
        retryable=False,
        idempotency_key=f"source-index-{uuid4()}",
    )
    run = RetrievalIndexRun(
        id=uuid4(),
        job_id=job.id,
        space_id=source.space_id,
        target_kind="source_version",
        target_id=version.id,
        scope_node_id=(await _root(session)).id,
        input_hash=_sha256(f"run:{version.id}"),
        source_version_id=version.id,
        parse_artifact_id=artifact.id,
        status="ready",
        index_config_version=settings.index_version,
        chunker_version=settings.chunker_version,
        embedding_provider="fake",
        embedding_model=settings.embedding_model,
        embedding_config={"dimensions": settings.embedding_dimensions},
        embedding_dimensions=settings.embedding_dimensions,
        chunk_count=1,
        embedded_count=1,
    )
    vector = (
        await FakeEmbeddingGateway(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        ).embed([content])
    ).vectors[0]
    chunk = RetrievalChunk(
        id=uuid4(),
        index_run_id=run.id,
        space_id=source.space_id,
        corpus_kind="source_evidence",
        index_config_version=settings.index_version,
        active=True,
        source_id=source.id,
        source_version_id=version.id,
        parse_artifact_id=artifact.id,
        section_id=section.id,
        ordinal=0,
        content_identity=_sha256(f"identity:{uuid4()}"),
        content_hash=_sha256(content),
        title=source.title,
        text=content,
        char_count=len(content),
        token_count=max(1, len(content.split())),
        path=path,
        heading_path=[source.title],
        locator={"page": 1},
        embedding_model=settings.embedding_model,
        embedding_config={"provider": "fake", "dimensions": settings.embedding_dimensions},
        embedding=vector,
    )
    session.add_all([job, run])
    await session.flush()
    session.add(chunk)
    await session.flush()
    return chunk


async def test_scope_filters_exact_descendants_siblings_spaces_and_stale_revisions(
    postgres_session: AsyncSession,
) -> None:
    settings = Settings(app_env="test")
    root = await _root(postgres_session)
    folder, folder_revision = await _create_node(
        postgres_session,
        parent=root,
        kind=KnowledgeNodeKind.FOLDER,
        title="Scoped folder",
    )
    document, current_revision = await _create_node(
        postgres_session,
        parent=folder,
        kind=KnowledgeNodeKind.DOCUMENT,
        title="Scoped document",
        body="scopeprobe current document",
    )
    sibling, sibling_revision = await _create_node(
        postgres_session,
        parent=root,
        kind=KnowledgeNodeKind.DOCUMENT,
        title="Sibling document",
        body="scopeprobe sibling document",
    )
    folder_chunk = await _add_knowledge_chunk(
        postgres_session,
        settings=settings,
        node=folder,
        revision=folder_revision,
        content="scopeprobe folder exact",
    )
    document_chunk = await _add_knowledge_chunk(
        postgres_session,
        settings=settings,
        node=document,
        revision=current_revision,
        content="scopeprobe current document",
    )
    await _add_knowledge_chunk(
        postgres_session,
        settings=settings,
        node=sibling,
        revision=sibling_revision,
        content="scopeprobe sibling document",
    )

    stale_revision = KnowledgeRevision(
        id=uuid4(),
        node_id=document.id,
        space_id=document.space_id,
        revision_number=2,
        title="Stale revision",
        body="scopeprobe stale revision",
        tags=[],
        conditions=[],
        exceptions=[],
        actor="local",
        content_hash=_sha256("stale revision"),
    )
    postgres_session.add(stale_revision)
    await postgres_session.flush()
    stale_chunk = await _add_knowledge_chunk(
        postgres_session,
        settings=settings,
        node=document,
        revision=stale_revision,
        content="scopeprobe stale revision",
    )

    other_space = KnowledgeSpace(id=uuid4(), slug=f"other-{uuid4()}", name="Other space")
    other_root = KnowledgeNode(
        id=uuid4(),
        space_id=other_space.id,
        parent_id=None,
        kind=KnowledgeNodeKind.ROOT.value,
        path=f"n{uuid4().hex}",
        version=1,
        sort_order=0,
    )
    other_root.path = f"n{other_root.id.hex}"
    postgres_session.add(other_space)
    await postgres_session.flush()
    postgres_session.add(other_root)
    await postgres_session.flush()
    other_revision = KnowledgeRevision(
        id=uuid4(),
        node_id=other_root.id,
        space_id=other_space.id,
        revision_number=1,
        title="Other root",
        body="scopeprobe other space",
        tags=[],
        conditions=[],
        exceptions=[],
        actor="local",
        content_hash=_sha256("other root"),
    )
    postgres_session.add(other_revision)
    await postgres_session.flush()
    other_root.current_revision_id = other_revision.id
    await postgres_session.flush()
    other_chunk = await _add_knowledge_chunk(
        postgres_session,
        settings=settings,
        node=other_root,
        revision=other_revision,
        content="scopeprobe other space",
        path=document.path,
    )

    resolver = ScopeResolver()
    service = DebugRetrievalService()
    exact = await resolver.resolve(
        postgres_session,
        settings=settings,
        space_id=DEFAULT_SPACE_ID,
        request=ScopeRequest(folder.id, False),
    )
    exact_hits = await service.keyword(
        postgres_session,
        settings=settings,
        scope=exact,
        query="scopeprobe",
        top_k=20,
    )
    assert [hit.chunk.id for hit in exact_hits] == [folder_chunk.id]

    descendants = await resolver.resolve(
        postgres_session,
        settings=settings,
        space_id=DEFAULT_SPACE_ID,
        request=ScopeRequest(folder.id, True),
    )
    keyword_hits = await service.keyword(
        postgres_session,
        settings=settings,
        scope=descendants,
        query="scopeprobe",
        top_k=20,
    )
    keyword_ids = {hit.chunk.id for hit in keyword_hits}
    assert keyword_ids == {folder_chunk.id, document_chunk.id}
    assert stale_chunk.id not in keyword_ids
    assert other_chunk.id not in keyword_ids

    vector_hits = await service.vector(
        postgres_session,
        settings=settings,
        scope=descendants,
        query="scopeprobe current document",
        top_k=20,
        gateway=FakeEmbeddingGateway(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        ),
    )
    vector_ids = {hit.chunk.id for hit in vector_hits}
    assert vector_ids == {folder_chunk.id, document_chunk.id}


async def test_source_scope_keeps_only_latest_ready_version(
    postgres_session: AsyncSession,
) -> None:
    settings = Settings(app_env="test")
    root = await _root(postgres_session)
    source = Source(
        id=uuid4(),
        space_id=DEFAULT_SPACE_ID,
        kind=SourceKind.PASTED_TEXT.value,
        title="Versioned source",
        status=SourceStatus.ACTIVE.value,
    )
    postgres_session.add(source)
    await postgres_session.flush()
    old_chunk = await _add_source_version(
        postgres_session,
        settings=settings,
        source=source,
        version_number=1,
        path=root.path,
        content="sourceprobe old version",
    )
    current_chunk = await _add_source_version(
        postgres_session,
        settings=settings,
        source=source,
        version_number=2,
        path=root.path,
        content="sourceprobe current version",
    )

    scope = await ScopeResolver().resolve(
        postgres_session,
        settings=settings,
        space_id=DEFAULT_SPACE_ID,
        request=ScopeRequest(root.id, True),
    )
    hits = await DebugRetrievalService().keyword(
        postgres_session,
        settings=settings,
        scope=scope,
        query="sourceprobe",
        top_k=20,
    )
    ids = {hit.chunk.id for hit in hits}
    assert current_chunk.id in ids
    assert old_chunk.id not in ids


async def test_rebuild_request_is_idempotent_for_same_target_and_configuration(
    postgres_session: AsyncSession,
) -> None:
    settings = Settings(app_env="test")
    root = await _root(postgres_session)
    document, revision = await _create_node(
        postgres_session,
        parent=root,
        kind=KnowledgeNodeKind.DOCUMENT,
        title="Idempotent document",
        body="idempotent retrieval content",
    )
    service = IndexingService()
    first = await service.request_knowledge_rebuild(
        postgres_session,
        settings=settings,
        space_id=DEFAULT_SPACE_ID,
        revision_id=revision.id,
        idempotency_key=f"index-rebuild-{uuid4()}",
    )
    replay = await service.request_knowledge_rebuild(
        postgres_session,
        settings=settings,
        space_id=DEFAULT_SPACE_ID,
        revision_id=revision.id,
        idempotency_key=f"index-rebuild-other-key-{uuid4()}",
    )

    assert first.run.id == replay.run.id
    assert first.job.id == replay.job.id
    run_count = await postgres_session.scalar(
        select(func.count()).select_from(RetrievalIndexRun).where(
            RetrievalIndexRun.space_id == DEFAULT_SPACE_ID,
            RetrievalIndexRun.target_kind == "knowledge_revision",
            RetrievalIndexRun.target_id == revision.id,
            RetrievalIndexRun.index_config_version == settings.index_version,
        )
    )
    assert run_count == 1
    assert document.current_revision_id == revision.id


async def test_keyword_retrieval_matches_chinese_bigrams(
    postgres_session: AsyncSession,
) -> None:
    settings = Settings(app_env="test")
    root = await _root(postgres_session)
    folder, _ = await _create_node(
        postgres_session,
        parent=root,
        kind=KnowledgeNodeKind.FOLDER,
        title="中文检索范围",
    )
    matching, matching_revision = await _create_node(
        postgres_session,
        parent=folder,
        kind=KnowledgeNodeKind.DOCUMENT,
        title="知识审核",
        body="每个新知识条目由创建者的直属领域负责人审核。",
    )
    other, other_revision = await _create_node(
        postgres_session,
        parent=folder,
        kind=KnowledgeNodeKind.DOCUMENT,
        title="项目晨会",
        body="项目晨会于北京时间九点四十五分开始。",
    )
    matching_chunk = await _add_knowledge_chunk(
        postgres_session,
        settings=settings,
        node=matching,
        revision=matching_revision,
        content="每个新知识条目由创建者的直属领域负责人审核。",
    )
    other_chunk = await _add_knowledge_chunk(
        postgres_session,
        settings=settings,
        node=other,
        revision=other_revision,
        content="项目晨会于北京时间九点四十五分开始。",
    )
    scope = await ScopeResolver().resolve(
        postgres_session,
        settings=settings,
        space_id=DEFAULT_SPACE_ID,
        request=ScopeRequest(folder.id, True),
    )

    hits = await DebugRetrievalService().keyword(
        postgres_session,
        settings=settings,
        scope=scope,
        query="文档中规定的知识条目审核人是谁？",
        top_k=5,
    )

    assert [hit.chunk.id for hit in hits] == [matching_chunk.id]
    assert all(hit.chunk.id != other_chunk.id for hit in hits)
    assert hits[0].score > 0


async def test_move_updates_chunk_path_and_delete_deactivates_subtree(
    postgres_session: AsyncSession,
) -> None:
    settings = Settings(app_env="test")
    service = KnowledgeTreeService()
    root = await _root(postgres_session)
    source_folder, _ = await _create_node(
        postgres_session,
        parent=root,
        kind=KnowledgeNodeKind.FOLDER,
        title="Move source",
    )
    destination, _ = await _create_node(
        postgres_session,
        parent=root,
        kind=KnowledgeNodeKind.FOLDER,
        title="Move destination",
    )
    document, revision = await _create_node(
        postgres_session,
        parent=source_folder,
        kind=KnowledgeNodeKind.DOCUMENT,
        title="Movable document",
        body="moveprobe document",
    )
    chunk = await _add_knowledge_chunk(
        postgres_session,
        settings=settings,
        node=document,
        revision=revision,
        content="moveprobe document",
    )
    old_path = document.path

    await service.move(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        node_id=document.id,
        idempotency_key=f"move-retrieval-{uuid4()}",
        request=KnowledgeNodeMove(
            expected_version=document.version,
            parent_id=destination.id,
        ),
    )
    await postgres_session.refresh(chunk)
    assert chunk.path != old_path
    assert chunk.path == document.path
    assert chunk.path.startswith(f"{destination.path}.")

    await service.delete(
        postgres_session,
        space_id=DEFAULT_SPACE_ID,
        node_id=destination.id,
        idempotency_key=f"delete-retrieval-{uuid4()}",
        request=KnowledgeNodeDelete(expected_version=destination.version),
    )
    await postgres_session.refresh(chunk)
    assert chunk.active is False
