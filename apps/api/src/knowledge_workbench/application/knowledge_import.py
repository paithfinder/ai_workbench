from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.knowledge_tree import (
    LOCAL_ACTOR,
    KnowledgeTreeService,
    revision_content_hash,
)
from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    ActivityEvent,
    ActorType,
    KnowledgeImportItem,
    KnowledgeImportRequest,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeRevision,
)

_ALLOWED_EXTENSIONS = {".md", ".txt"}


class KnowledgeImportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1)
    body: str


class KnowledgeImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: UUID | None = None
    expected_parent_version: int = Field(gt=0)
    root_name: str = Field(min_length=1, max_length=500)
    documents: list[KnowledgeImportDocument] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    relative_path: str
    parts: tuple[str, ...]
    body: str
    body_utf8_bytes: int


@dataclass(frozen=True, slots=True)
class PreparedImport:
    root_name: str
    documents: tuple[PreparedDocument, ...]
    folders: tuple[tuple[str, ...], ...]
    entry_count: int
    folder_count: int
    document_count: int
    total_body_utf8_bytes: int
    request_hash: str


@dataclass(frozen=True, slots=True)
class KnowledgeImportItemOutcome:
    ordinal: int
    relative_path: str
    kind: str
    node_id: UUID
    revision_id: UUID
    content_hash: str


@dataclass(frozen=True, slots=True)
class KnowledgeImportOutcome:
    request_id: UUID
    idempotency_key: str
    root_node_id: UUID
    entry_count: int
    folder_count: int
    document_count: int
    total_body_utf8_bytes: int
    items: tuple[KnowledgeImportItemOutcome, ...]


def _invalid(message: str, *, details: list[dict[str, object]] | None = None) -> AppError:
    return AppError(
        "invalid_knowledge_import",
        message,
        status_code=422,
        details=details or [],
    )


def _normalize_segment(value: str, *, field: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized in {".", ".."}:
        raise _invalid(f"{field} contains an empty or reserved path segment.")
    if "/" in normalized or "\\" in normalized:
        raise _invalid(f"{field} must not contain path separators.")
    if normalized != normalized.strip():
        raise _invalid(f"{field} path segments must not start or end with whitespace.")
    if len(normalized) > 500:
        raise _invalid(f"{field} path segments must not exceed 500 characters.")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise _invalid(f"{field} contains control characters.")
    return normalized


def _normalize_relative_path(value: str, *, max_characters: int) -> tuple[str, ...]:
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        raise _invalid("relative_path must be a relative POSIX path.")
    if len(value) > max_characters:
        raise _invalid(
            f"relative_path must not exceed {max_characters} characters."
        )
    raw_parts = value.split("/")
    if any(part == "" for part in raw_parts):
        raise _invalid("relative_path contains an empty path segment.")
    if len(raw_parts[0]) >= 2 and raw_parts[0][1] == ":":
        raise _invalid("relative_path must not contain a drive prefix.")
    parts = tuple(
        _normalize_segment(part, field="relative_path") for part in raw_parts
    )
    suffix = PurePosixPath(parts[-1]).suffix.casefold()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise _invalid("Only Markdown (.md) and text (.txt) documents can be imported.")
    return parts


def prepare_knowledge_import(
    request: KnowledgeImportCreate,
    *,
    settings: Settings,
) -> PreparedImport:
    root_name = _normalize_segment(request.root_name, field="root_name")
    if len(request.documents) > settings.knowledge_import_max_entries:
        raise _invalid(
            "Knowledge imports can contain at most "
            f"{settings.knowledge_import_max_entries} documents."
        )
    documents: list[PreparedDocument] = []
    folders: set[tuple[str, ...]] = set()
    document_keys: set[tuple[str, ...]] = set()
    original_paths: dict[tuple[str, ...], str] = {}
    total_body_bytes = 0

    for document in request.documents:
        parts = _normalize_relative_path(
            document.relative_path,
            max_characters=settings.knowledge_import_max_relative_path_characters,
        )
        if len(parts) + 1 > settings.knowledge_import_max_depth:
            raise _invalid(
                "Knowledge import paths must not exceed "
                f"{settings.knowledge_import_max_depth} levels."
            )
        normalized_path = "/".join(parts)
        if len(normalized_path) > settings.knowledge_import_max_relative_path_characters:
            raise _invalid(
                "relative_path must not exceed "
                f"{settings.knowledge_import_max_relative_path_characters} characters."
            )
        key = tuple(part.casefold() for part in parts)
        if key in document_keys:
            raise _invalid(
                "The import contains duplicate document paths.",
                details=[
                    {
                        "relative_path": normalized_path,
                        "conflicts_with": original_paths[key],
                    }
                ],
            )
        document_keys.add(key)
        original_paths[key] = normalized_path
        for index in range(1, len(parts)):
            folders.add(parts[:index])
        body_bytes = len(document.body.encode("utf-8"))
        if len(document.body) > settings.knowledge_import_max_document_characters:
            raise _invalid(
                "Knowledge import documents must not exceed "
                f"{settings.knowledge_import_max_document_characters} characters."
            )
        total_body_bytes += body_bytes
        documents.append(
            PreparedDocument(
                relative_path=normalized_path,
                parts=parts,
                body=document.body,
                body_utf8_bytes=body_bytes,
            )
        )

    folder_keys: dict[tuple[str, ...], tuple[str, ...]] = {}
    for folder in folders:
        key = tuple(part.casefold() for part in folder)
        previous = folder_keys.get(key)
        if previous is not None and previous != folder:
            raise _invalid("The import contains ambiguous folder paths.")
        folder_keys[key] = folder
        if key in document_keys:
            raise _invalid("A path cannot be both a folder and a document.")

    sorted_folders = tuple(
        sorted(folders, key=lambda path: (len(path), tuple(part.casefold() for part in path), path))
    )
    sorted_documents = tuple(
        sorted(
            documents,
            key=lambda item: (tuple(part.casefold() for part in item.parts), item.parts),
        )
    )
    folder_count = len(sorted_folders) + 1
    document_count = len(sorted_documents)
    entry_count = folder_count + document_count
    if entry_count > settings.knowledge_import_max_entries:
        raise _invalid(
            f"Knowledge imports can contain at most {settings.knowledge_import_max_entries} nodes."
        )
    if folder_count > settings.knowledge_import_max_folders:
        raise _invalid(
            "Knowledge imports can contain at most "
            f"{settings.knowledge_import_max_folders} folders."
        )
    if total_body_bytes > settings.knowledge_import_max_total_body_bytes:
        raise AppError(
            "knowledge_import_too_large",
            "Knowledge import document content exceeds the configured size limit.",
            status_code=413,
        )

    canonical = {
        "documents": [
            {
                "body_sha256": hashlib.sha256(item.body.encode("utf-8")).hexdigest(),
                "body_utf8_bytes": item.body_utf8_bytes,
                "relative_path": item.relative_path,
            }
            for item in sorted_documents
        ],
        "expected_parent_version": request.expected_parent_version,
        "parent_id": str(request.parent_id) if request.parent_id else None,
        "root_name": root_name,
        "schema_version": 1,
    }
    request_hash = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return PreparedImport(
        root_name=root_name,
        documents=sorted_documents,
        folders=sorted_folders,
        entry_count=entry_count,
        folder_count=folder_count,
        document_count=document_count,
        total_body_utf8_bytes=total_body_bytes,
        request_hash=request_hash,
    )


class KnowledgeImportService:
    async def create(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        idempotency_key: str,
        request: KnowledgeImportCreate,
        settings: Settings,
    ) -> KnowledgeImportOutcome:
        key = _validate_idempotency_key(idempotency_key)
        prepared = prepare_knowledge_import(request, settings=settings)
        replay = await self._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=prepared.request_hash,
        )
        if replay is not None:
            return replay

        await KnowledgeTreeService._lock_space(session, space_id)
        replay = await self._replay(
            session,
            space_id=space_id,
            key=key,
            request_hash=prepared.request_hash,
        )
        if replay is not None:
            return replay
        parent = await KnowledgeTreeService._lock_parent(
            session, space_id=space_id, parent_id=request.parent_id
        )
        KnowledgeTreeService._validate_version(parent, request.expected_parent_version)
        KnowledgeTreeService._validate_parent(KnowledgeNodeKind.FOLDER.value, parent)

        first_sort_order = await session.scalar(
            select(func.max(KnowledgeNode.sort_order)).where(
                KnowledgeNode.space_id == space_id,
                KnowledgeNode.parent_id == parent.id,
                KnowledgeNode.deleted_at.is_(None),
            )
        )
        root_node = KnowledgeNode(
            id=uuid4(),
            space_id=space_id,
            parent_id=parent.id,
            kind=KnowledgeNodeKind.FOLDER.value,
            path="pending",
            version=1,
            sort_order=(first_sort_order if first_sort_order is not None else -1) + 1,
        )
        root_node.path = f"{parent.path}.n{root_node.id.hex}"
        session.add(root_node)
        await session.flush()
        root_revision = self._new_revision(root_node, title=prepared.root_name, body="")
        session.add(root_revision)
        await session.flush()
        root_node.current_revision_id = root_revision.id

        nodes_by_path: dict[tuple[str, ...], KnowledgeNode] = {(): root_node}
        revisions_by_path: dict[tuple[str, ...], KnowledgeRevision] = {(): root_revision}
        children_counts: dict[tuple[str, ...], int] = {}
        for folder_path in prepared.folders:
            parent_path = folder_path[:-1]
            folder_parent = nodes_by_path[parent_path]
            sort_order = children_counts.get(parent_path, 0)
            children_counts[parent_path] = sort_order + 1
            node = KnowledgeNode(
                id=uuid4(),
                space_id=space_id,
                parent_id=folder_parent.id,
                kind=KnowledgeNodeKind.FOLDER.value,
                path=f"{folder_parent.path}.pending",
                version=1,
                sort_order=sort_order,
            )
            node.path = f"{folder_parent.path}.n{node.id.hex}"
            session.add(node)
            await session.flush()
            revision = self._new_revision(node, title=folder_path[-1], body="")
            session.add(revision)
            await session.flush()
            node.current_revision_id = revision.id
            nodes_by_path[folder_path] = node
            revisions_by_path[folder_path] = revision

        document_records: list[tuple[PreparedDocument, KnowledgeNode, KnowledgeRevision]] = []
        for document in prepared.documents:
            parent_path = document.parts[:-1]
            document_parent = nodes_by_path[parent_path]
            sort_order = children_counts.get(parent_path, 0)
            children_counts[parent_path] = sort_order + 1
            node = KnowledgeNode(
                id=uuid4(),
                space_id=space_id,
                parent_id=document_parent.id,
                kind=KnowledgeNodeKind.DOCUMENT.value,
                path=f"{document_parent.path}.pending",
                version=1,
                sort_order=sort_order,
            )
            node.path = f"{document_parent.path}.n{node.id.hex}"
            session.add(node)
            await session.flush()
            revision = self._new_revision(
                node, title=document.parts[-1], body=document.body
            )
            session.add(revision)
            await session.flush()
            node.current_revision_id = revision.id
            document_records.append((document, node, revision))

        request_row = KnowledgeImportRequest(
            id=uuid4(),
            space_id=space_id,
            idempotency_key=key,
            request_hash=prepared.request_hash,
            root_node_id=root_node.id,
            entry_count=prepared.entry_count,
            folder_count=prepared.folder_count,
            document_count=prepared.document_count,
            total_body_utf8_bytes=prepared.total_body_utf8_bytes,
        )
        session.add(request_row)
        await session.flush()

        records: list[tuple[str, str, KnowledgeNode, KnowledgeRevision, int]] = [
            (".", KnowledgeNodeKind.FOLDER.value, root_node, root_revision, 0)
        ]
        records.extend(
            (
                "/".join(path),
                KnowledgeNodeKind.FOLDER.value,
                nodes_by_path[path],
                revisions_by_path[path],
                0,
            )
            for path in prepared.folders
        )
        records.extend(
            (
                document.relative_path,
                KnowledgeNodeKind.DOCUMENT.value,
                node,
                revision,
                document.body_utf8_bytes,
            )
            for document, node, revision in document_records
        )
        items = [
            KnowledgeImportItem(
                id=uuid4(),
                request_id=request_row.id,
                ordinal=ordinal,
                relative_path=relative_path,
                kind=kind,
                node_id=node.id,
                revision_id=revision.id,
                body_utf8_bytes=body_bytes,
                content_hash=revision.content_hash,
            )
            for ordinal, (relative_path, kind, node, revision, body_bytes) in enumerate(records)
        ]
        session.add_all(items)
        session.add(
            ActivityEvent(
                id=uuid4(),
                space_id=space_id,
                event_type="knowledge_import.succeeded",
                entity_type="knowledge_import",
                entity_id=request_row.id,
                actor_type=ActorType.USER.value,
                payload={
                    "root_node_id": str(root_node.id),
                    "entry_count": prepared.entry_count,
                    "folder_count": prepared.folder_count,
                    "document_count": prepared.document_count,
                    "total_body_utf8_bytes": prepared.total_body_utf8_bytes,
                },
            )
        )
        await session.flush()
        return self._outcome(request_row, items)

    async def get_request(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        idempotency_key: str,
    ) -> KnowledgeImportOutcome:
        key = _validate_idempotency_key(idempotency_key)
        stored = await self._stored(session, space_id=space_id, key=key)
        if stored is None:
            raise AppError(
                "knowledge_import_request_not_found",
                "Knowledge import request was not found.",
                status_code=404,
            )
        return self._outcome(*stored)

    async def _replay(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        key: str,
        request_hash: str,
    ) -> KnowledgeImportOutcome | None:
        stored = await self._stored(session, space_id=space_id, key=key)
        if stored is None:
            return None
        request_row, items = stored
        if request_row.request_hash != request_hash:
            raise AppError(
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request.",
                status_code=409,
            )
        return self._outcome(request_row, items)

    @staticmethod
    async def _stored(
        session: AsyncSession,
        *,
        space_id: UUID,
        key: str,
    ) -> tuple[KnowledgeImportRequest, list[KnowledgeImportItem]] | None:
        request_row = await session.scalar(
            select(KnowledgeImportRequest).where(
                KnowledgeImportRequest.space_id == space_id,
                KnowledgeImportRequest.idempotency_key == key,
            )
        )
        if request_row is None:
            return None
        items = list(
            await session.scalars(
                select(KnowledgeImportItem)
                .where(KnowledgeImportItem.request_id == request_row.id)
                .order_by(KnowledgeImportItem.ordinal)
            )
        )
        return request_row, items

    @staticmethod
    def _new_revision(
        node: KnowledgeNode, *, title: str, body: str
    ) -> KnowledgeRevision:
        return KnowledgeRevision(
            id=uuid4(),
            node_id=node.id,
            space_id=node.space_id,
            revision_number=1,
            title=title,
            body=body,
            tags=[],
            conditions=[],
            exceptions=[],
            actor=LOCAL_ACTOR,
            content_hash=revision_content_hash(
                title=title,
                body=body,
                tags=[],
                conditions=[],
                exceptions=[],
            ),
            edit_reason=None,
        )

    @staticmethod
    def _outcome(
        request_row: KnowledgeImportRequest,
        items: list[KnowledgeImportItem],
    ) -> KnowledgeImportOutcome:
        return KnowledgeImportOutcome(
            request_id=request_row.id,
            idempotency_key=request_row.idempotency_key,
            root_node_id=request_row.root_node_id,
            entry_count=request_row.entry_count,
            folder_count=request_row.folder_count,
            document_count=request_row.document_count,
            total_body_utf8_bytes=request_row.total_body_utf8_bytes,
            items=tuple(
                KnowledgeImportItemOutcome(
                    ordinal=item.ordinal,
                    relative_path=item.relative_path,
                    kind=item.kind,
                    node_id=item.node_id,
                    revision_id=item.revision_id,
                    content_hash=item.content_hash,
                )
                for item in items
            ),
        )
