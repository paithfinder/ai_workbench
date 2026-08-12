#!/usr/bin/env python3
"""Prepare a deterministic PostgreSQL fixture for the real D7 retrieval baseline."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from knowledge_workbench.application.indexing import IndexingService
from knowledge_workbench.application.ports.embedding_gateway import EmbeddingError
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobKind,
    KnowledgeNode,
    KnowledgeRevision,
    KnowledgeSpace,
    RetrievalChunk,
    RetrievalIndexRun,
    Source,
    SourceParseArtifact,
    SourceSection,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine, create_session_factory
from knowledge_workbench.worker.job_runner import JobAttemptError
from knowledge_workbench.worker.job_runner import (
    release_transient_attempt as release_index_attempt,
)
from knowledge_workbench.worker.source_index import (
    SourceIndexWorker,
    create_embedding_gateway,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = BASE_DIR / "rag-seeds.zh-CN.json"
DEFAULT_FIXTURE_MAP = BASE_DIR / "fixture-map.local.json"
EXPECTED_DATABASE_REVISION = "0009_d7_cjk_fts"
FIXTURE_NAMESPACE = "https://zixu.local/evals/d7-retrieval/"
OUTSIDE_SCOPE_KEY = "__out_of_scope__"


@dataclass(frozen=True, slots=True)
class FixtureUnit:
    scope_key: str
    content_key: str
    section_key: str | None
    text: str
    seed_id: str
    outside: bool = False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--fixture-map", type=Path, default=DEFAULT_FIXTURE_MAP)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="check BGE-M3, PostgreSQL, and an existing fixture without writing data",
    )
    return parser.parse_args(argv)


def _load_dataset(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or not isinstance(value.get("seeds"), list):
        raise TypeError("dataset root must contain a seeds array")
    if not isinstance(value.get("dataset"), str) or not isinstance(value.get("version"), str):
        raise TypeError("dataset and version must be strings")
    return value


def fixture_units(dataset: dict[str, Any]) -> list[FixtureUnit]:
    units: list[FixtureUnit] = []
    seen_content: set[str] = set()
    seen_sections: set[str] = set()
    for seed in dataset["seeds"]:
        if seed.get("should_abstain"):
            continue
        contexts = seed["contexts"]
        identities = seed["expected_content_identities"]
        sections = seed["expected_section_ids"]
        if len(identities) != len(sections):
            raise ValueError(f"{seed['id']} must pair every expected identity with a section")
        if len(identities) == len(contexts):
            positive_texts = contexts
        elif len(identities) == 1:
            positive_texts = ["\n".join(contexts)]
        else:
            raise ValueError(
                f"{seed['id']} contexts cannot be assigned deterministically to expected identities"
            )
        for identity, section, content in zip(
            identities, sections, positive_texts, strict=True
        ):
            if identity in seen_content or section in seen_sections:
                raise ValueError(f"duplicate fixture key in {seed['id']}: {identity} / {section}")
            seen_content.add(identity)
            seen_sections.add(section)
            units.append(
                FixtureUnit(
                    scope_key=seed["scope"]["key"],
                    content_key=identity,
                    section_key=section,
                    text=content,
                    seed_id=seed["id"],
                )
            )
        outside_text = "\n".join([seed["question"], *contexts])
        for outside_identity in seed["out_of_scope_content_identities"]:
            if outside_identity in seen_content:
                raise ValueError(f"duplicate fixture key in {seed['id']}: {outside_identity}")
            seen_content.add(outside_identity)
            units.append(
                FixtureUnit(
                    scope_key=OUTSIDE_SCOPE_KEY,
                    content_key=outside_identity,
                    section_key=None,
                    text=outside_text,
                    seed_id=seed["id"],
                    outside=True,
                )
            )
    if not units:
        raise ValueError("dataset has no answerable retrieval fixture units")
    return units


def _stable_uuid(dataset: dict[str, Any], kind: str, key: str) -> UUID:
    identity = f"{dataset['dataset']}:{dataset['version']}:{kind}:{key}"
    return uuid5(NAMESPACE_URL, FIXTURE_NAMESPACE + identity)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _slug(dataset: dict[str, Any]) -> str:
    version = re.sub(r"[^a-z0-9]+", "-", dataset["version"].lower()).strip("-")
    digest = _sha256(f"{dataset['dataset']}:{dataset['version']}")[:10]
    return f"d7-retrieval-eval-{version}-{digest}"[:100]


def _source_payload(units: list[FixtureUnit]) -> str:
    return "\n\n".join(f"[{unit.content_key}]\n{unit.text}" for unit in units)


async def _require_database(session: AsyncSession) -> None:
    revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != EXPECTED_DATABASE_REVISION:
        raise RuntimeError(
            f"PostgreSQL must be migrated through {EXPECTED_DATABASE_REVISION} "
            f"(current: {revision!r})"
        )
    if await session.scalar(text("SELECT to_regclass('public.retrieval_chunks')")) is None:
        raise RuntimeError("PostgreSQL has no retrieval_chunks table")


async def _probe_embedding(settings: Settings) -> None:
    if settings.embedding_provider != "bge_m3_http":
        raise RuntimeError(
            "real fixture preparation requires EMBEDDING_PROVIDER=bge_m3_http; "
            "Fake cannot produce a baseline"
        )
    if "bge-m3" not in settings.embedding_model.lower():
        raise RuntimeError("EMBEDDING_MODEL must identify BGE-M3")
    result = await create_embedding_gateway(settings).embed(["D7 BGE-M3 readiness probe"])
    if (
        result.provider != "bge_m3_http"
        or "bge-m3" not in result.model.lower()
        or result.dimensions != 1024
        or len(result.vectors) != 1
    ):
        raise RuntimeError("BGE-M3 readiness probe returned ineligible metadata")


async def _ensure_folder(
    session: AsyncSession,
    *,
    dataset: dict[str, Any],
    space: KnowledgeSpace,
    root: KnowledgeNode,
    scope_key: str,
    sort_order: int,
) -> KnowledgeNode:
    node_id = _stable_uuid(dataset, "scope", scope_key)
    node = await session.get(KnowledgeNode, node_id)
    if node is None:
        node = KnowledgeNode(
            id=node_id,
            space_id=space.id,
            parent_id=root.id,
            kind="folder",
            path=f"{root.path}.n{node_id.hex}",
            version=1,
            sort_order=sort_order,
        )
        session.add(node)
        await session.flush()
        title = "范围外泄漏对照" if scope_key == OUTSIDE_SCOPE_KEY else scope_key
        revision = KnowledgeRevision(
            id=_stable_uuid(dataset, "scope-revision", scope_key),
            node_id=node.id,
            space_id=space.id,
            revision_number=1,
            title=title,
            body="D7 retrieval evaluation scope",
            tags=["d7-eval"],
            conditions=[],
            exceptions=[],
            actor="d7-eval-loader",
            content_hash=_sha256(f"{title}:D7 retrieval evaluation scope"),
        )
        session.add(revision)
        await session.flush()
        node.current_revision_id = revision.id
        await session.flush()
    if node.space_id != space.id or node.parent_id != root.id or node.kind != "folder":
        raise RuntimeError(f"existing scope node is inconsistent: {scope_key}")
    return node


async def _ensure_source(
    session: AsyncSession,
    *,
    dataset: dict[str, Any],
    space: KnowledgeSpace,
    folder: KnowledgeNode,
    scope_key: str,
    units: list[FixtureUnit],
) -> SourceVersion:
    source_id = _stable_uuid(dataset, "source", scope_key)
    version_id = _stable_uuid(dataset, "source-version", scope_key)
    artifact_id = _stable_uuid(dataset, "parse-artifact", scope_key)
    payload = _source_payload(units)
    payload_hash = _sha256(payload)
    source = await session.get(Source, source_id)
    if source is None:
        source = Source(
            id=source_id,
            space_id=space.id,
            kind="pasted_text",
            title=f"D7 eval · {scope_key}",
            status="active",
        )
        session.add(source)
        await session.flush()
    version = await session.get(SourceVersion, version_id)
    if version is None:
        version = SourceVersion(
            id=version_id,
            source_id=source.id,
            version_number=1,
            content_sha256=payload_hash,
            storage_key=f"evals/{dataset['version']}/{scope_key}.txt",
            object_etag=payload_hash,
            acquisition_type="pasted_text",
            acquisition_metadata={"dataset": dataset["dataset"], "version": dataset["version"]},
            processing_status="ready",
            parse_status="ready",
        )
        session.add(version)
        await session.flush()
    elif version.content_sha256 != payload_hash:
        raise RuntimeError(
            f"dataset content changed without a version bump for scope {scope_key}"
        )
    artifact = await session.get(SourceParseArtifact, artifact_id)
    if artifact is None:
        artifact = SourceParseArtifact(
            id=artifact_id,
            source_version_id=version.id,
            revision=1,
            parser_name="d7-eval-loader",
            parser_version="1",
            parser_config={"deterministic": True},
            status="ready",
            canonical_content_sha256=payload_hash,
            warnings=[],
            artifact_metadata={"fixture": True},
        )
        session.add(artifact)
        await session.flush()
    version.current_parse_artifact_id = artifact.id
    for ordinal, unit in enumerate(units):
        section_id = _stable_uuid(dataset, "section", unit.content_key)
        section = await session.get(SourceSection, section_id)
        content_hash = _sha256(unit.text)
        if section is None:
            session.add(
                SourceSection(
                    id=section_id,
                    parse_artifact_id=artifact.id,
                    source_version_id=version.id,
                    space_id=space.id,
                    block_id=f"eval-{ordinal:03d}-{_sha256(unit.content_key)[:12]}",
                    ordinal=ordinal,
                    block_type="paragraph",
                    title=unit.content_key,
                    text=unit.text,
                    heading_path=[scope_key, unit.seed_id],
                    paragraph_index=ordinal,
                    locator={"seed_id": unit.seed_id, "content_key": unit.content_key},
                    quote_hash=content_hash,
                    content_hash=content_hash,
                    provenance={"dataset": dataset["dataset"], "version": dataset["version"]},
                )
            )
        elif section.content_hash != content_hash or section.parse_artifact_id != artifact.id:
            raise RuntimeError(
                f"dataset section changed without a version bump: {unit.content_key}"
            )
    node_id = _stable_uuid(dataset, "source-node", scope_key)
    node = await session.get(KnowledgeNode, node_id)
    if node is None:
        session.add(
            KnowledgeNode(
                id=node_id,
                space_id=space.id,
                parent_id=folder.id,
                kind="source",
                path=f"{folder.path}.n{node_id.hex}",
                version=1,
                sort_order=0,
                source_id=source.id,
                source_version_id=version.id,
            )
        )
    await session.flush()
    return version


async def _ensure_fixture_rows(
    sessions: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    dataset: dict[str, Any],
    units: list[FixtureUnit],
) -> tuple[UUID, list[UUID]]:
    space_id = _stable_uuid(dataset, "space", "root")
    grouped: dict[str, list[FixtureUnit]] = {}
    for unit in units:
        grouped.setdefault(unit.scope_key, []).append(unit)
    version_ids: list[UUID] = []
    async with sessions() as session, session.begin():
        await _require_database(session)
        space = await session.get(KnowledgeSpace, space_id)
        if space is None:
            space = KnowledgeSpace(
                id=space_id,
                slug=_slug(dataset),
                name=f"D7 Retrieval Eval {dataset['version']}",
            )
            session.add(space)
            await session.flush()
        elif space.slug != _slug(dataset):
            raise RuntimeError("existing evaluation space identity is inconsistent")
        root_id = _stable_uuid(dataset, "root", "root")
        root = await session.get(KnowledgeNode, root_id)
        if root is None:
            root = KnowledgeNode(
                id=root_id,
                space_id=space.id,
                parent_id=None,
                kind="root",
                path=f"n{root_id.hex}",
                version=1,
                sort_order=0,
            )
            session.add(root)
            await session.flush()
        for sort_order, scope_key in enumerate(sorted(grouped), 1):
            folder = await _ensure_folder(
                session,
                dataset=dataset,
                space=space,
                root=root,
                scope_key=scope_key,
                sort_order=sort_order,
            )
            version = await _ensure_source(
                session,
                dataset=dataset,
                space=space,
                folder=folder,
                scope_key=scope_key,
                units=grouped[scope_key],
            )
            version_ids.append(version.id)
        service = IndexingService()
        for version_id in version_ids:
            await service.request_source_rebuild(
                session,
                settings=settings,
                space_id=space.id,
                source_version_id=version_id,
                idempotency_key=f"d7-eval:{dataset['version']}:{version_id}",
            )
    return space_id, version_ids


async def _run_pending_indexes(
    sessions: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    space_id: UUID,
    version_ids: list[UUID],
) -> None:
    gateway = create_embedding_gateway(settings)
    worker = SourceIndexWorker(settings, gateway, heartbeat_session_factory=sessions)
    async with sessions() as session:
        rows = (
            await session.execute(
                select(RetrievalIndexRun, Job)
                .join(Job, Job.id == RetrievalIndexRun.job_id)
                .where(
                    RetrievalIndexRun.space_id == space_id,
                    RetrievalIndexRun.source_version_id.in_(version_ids),
                    RetrievalIndexRun.index_config_version == settings.index_version,
                )
            )
        ).all()
    for run, job in rows:
        if run.status == "ready" and job.status == "succeeded":
            continue
        if run.status != "queued" or job.status != "queued":
            raise RuntimeError(
                f"index run {run.id} is {run.status}/{job.status}; resolve it before rerunning"
            )
        async with sessions() as session:
            try:
                await worker.run(
                    session,
                    job_id=job.id,
                    celery_task_id="d7-eval-loader",
                    worker_name="d7-eval-loader",
                )
            except JobAttemptError as exc:
                await release_index_attempt(
                    settings,
                    job_kind=JobKind.SOURCE_INDEX,
                    job_id=job.id,
                    token=exc.token,
                    message=str(exc),
                )
                raise RuntimeError(
                    f"index run {run.id} failed transiently and was returned to queued: {exc}"
                ) from exc
    async with sessions() as session:
        failed = (
            await session.execute(
                select(RetrievalIndexRun.id, RetrievalIndexRun.status, RetrievalIndexRun.error_message)
                .where(
                    RetrievalIndexRun.space_id == space_id,
                    RetrievalIndexRun.source_version_id.in_(version_ids),
                    RetrievalIndexRun.index_config_version == settings.index_version,
                    RetrievalIndexRun.status != "ready",
                )
            )
        ).all()
    if failed:
        raise RuntimeError(f"fixture indexing did not become ready: {failed}")


async def _fixture_map(
    sessions: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    dataset: dict[str, Any],
    units: list[FixtureUnit],
) -> dict[str, Any]:
    space_id = _stable_uuid(dataset, "space", "root")
    scopes = sorted({unit.scope_key for unit in units if not unit.outside})
    mapping: dict[str, dict[str, str]] = {
        "scope_node_ids": {
            key: str(_stable_uuid(dataset, "scope", key)) for key in scopes
        },
        "content_identities": {},
        "section_ids": {},
    }
    async with sessions() as session:
        await _require_database(session)
        for unit in units:
            section_id = _stable_uuid(dataset, "section", unit.content_key)
            chunks = list(
                await session.scalars(
                    select(RetrievalChunk).where(
                        RetrievalChunk.space_id == space_id,
                        RetrievalChunk.section_id == section_id,
                        RetrievalChunk.index_config_version == settings.index_version,
                        RetrievalChunk.embedding_model == settings.embedding_model,
                        RetrievalChunk.active.is_(True),
                    )
                )
            )
            if len(chunks) != 1:
                raise RuntimeError(
                    f"expected one active chunk for {unit.content_key}, found {len(chunks)}"
                )
            mapping["content_identities"][unit.content_key] = chunks[0].content_identity
            if unit.section_key is not None:
                mapping["section_ids"][unit.section_key] = str(section_id)
    return {
        "dataset": dataset["dataset"],
        "version": dataset["version"],
        "space_id": str(space_id),
        "embedding": {
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "dimensions": settings.embedding_dimensions,
        },
        "index_config_version": settings.index_version,
        "mappings": mapping,
    }


async def prepare(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    dataset = _load_dataset(args.dataset)
    units = fixture_units(dataset)
    await _probe_embedding(settings)
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        if args.check_only:
            result = await _fixture_map(
                sessions,
                settings=settings,
                dataset=dataset,
                units=units,
            )
        else:
            space_id, version_ids = await _ensure_fixture_rows(
                sessions,
                settings=settings,
                dataset=dataset,
                units=units,
            )
            await _run_pending_indexes(
                sessions,
                settings=settings,
                space_id=space_id,
                version_ids=version_ids,
            )
            result = await _fixture_map(
                sessions,
                settings=settings,
                dataset=dataset,
                units=units,
            )
    finally:
        await engine.dispose()
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(prepare(args))
    except (
        EmbeddingError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"retrieval fixture preparation failed: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if not args.check_only:
        args.fixture_map.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
