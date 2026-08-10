from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, status
from pydantic import BaseModel

from knowledge_workbench.api.dependencies import SessionDependency
from knowledge_workbench.application.knowledge_tree import (
    KnowledgeEvidenceRecord,
    KnowledgeNodeCreate,
    KnowledgeNodeDelete,
    KnowledgeNodeDetail,
    KnowledgeNodeEdit,
    KnowledgeNodeMove,
    KnowledgeNodeRecord,
    KnowledgeSearchRecord,
    KnowledgeTreeService,
    KnowledgeWriteOutcome,
)
from knowledge_workbench.core.errors import ErrorEnvelope

router = APIRouter(prefix="/api/v1/knowledge-spaces/{space_id}", tags=["knowledge-tree"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 422)
}


class KnowledgeRevisionResponse(BaseModel):
    id: UUID
    node_id: UUID
    revision_number: int
    title: str
    body: str
    tags: list[str]
    conditions: list[str]
    exceptions: list[str]
    content_hash: str
    actor: str
    edit_reason: str | None
    created_at: datetime


class KnowledgeNodeResponse(BaseModel):
    id: UUID
    space_id: UUID
    parent_id: UUID | None
    kind: str
    path: str
    version: int
    sort_order: int
    origin_candidate_id: UUID | None
    current_revision_id: UUID | None
    source_id: UUID | None
    source_version_id: UUID | None
    title: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class KnowledgeTreeResponse(BaseModel):
    items: list[KnowledgeNodeResponse]


class KnowledgeSearchItemResponse(BaseModel):
    node: KnowledgeNodeResponse
    breadcrumb: str
    ancestor_ids: list[UUID]
    match_fields: list[str]


class KnowledgeSearchResponse(BaseModel):
    items: list[KnowledgeSearchItemResponse]


class KnowledgeEvidenceResponse(BaseModel):
    id: UUID
    node_id: UUID
    revision_id: UUID
    revision_number: int
    source_id: UUID
    source_title: str
    source_version_id: UUID
    source_version_number: int
    source_content_hash: str | None
    parse_artifact_id: UUID
    artifact_revision: int
    section_id: UUID
    section_ordinal: int
    quote_hash: str
    content_hash: str
    locator: dict[str, Any]
    frozen_quote: str
    deep_link: str
    anchor_status: str = "exact"
    created_at: datetime


class KnowledgeNodeDetailResponse(BaseModel):
    node: KnowledgeNodeResponse
    revision: KnowledgeRevisionResponse | None
    evidence: list[KnowledgeEvidenceResponse]


class KnowledgeWriteResponse(BaseModel):
    result_id: UUID
    request_id: UUID
    node_id: UUID
    node_version: int
    snapshot: dict[str, Any]


class KnowledgeWriteRequestResponse(BaseModel):
    idempotency_key: str
    operation: str
    status: str = "succeeded"
    result: KnowledgeWriteResponse


def _node_response(record: KnowledgeNodeRecord | KnowledgeNodeDetail) -> KnowledgeNodeResponse:
    node = record.node
    title = record.revision.title if record.revision is not None else record.source_title
    return KnowledgeNodeResponse(
        id=node.id,
        space_id=node.space_id,
        parent_id=node.parent_id,
        kind=node.kind,
        path=node.path,
        version=node.version,
        sort_order=node.sort_order,
        origin_candidate_id=node.origin_candidate_id,
        current_revision_id=node.current_revision_id,
        source_id=node.source_id,
        source_version_id=node.source_version_id,
        title=title,
        created_at=node.created_at,
        updated_at=node.updated_at,
        deleted_at=node.deleted_at,
    )


def _search_response(record: KnowledgeSearchRecord) -> KnowledgeSearchItemResponse:
    return KnowledgeSearchItemResponse(
        node=_node_response(record.record),
        breadcrumb=record.breadcrumb,
        ancestor_ids=record.ancestor_ids,
        match_fields=record.match_fields,
    )


def _revision_response(detail: KnowledgeNodeDetail) -> KnowledgeRevisionResponse | None:
    revision = detail.revision
    if revision is None:
        return None
    return KnowledgeRevisionResponse(
        id=revision.id,
        node_id=revision.node_id,
        revision_number=revision.revision_number,
        title=revision.title,
        body=revision.body,
        tags=list(revision.tags),
        conditions=list(revision.conditions),
        exceptions=list(revision.exceptions),
        content_hash=revision.content_hash,
        actor=revision.actor,
        edit_reason=revision.edit_reason,
        created_at=revision.created_at,
    )


def _evidence_response(record: KnowledgeEvidenceRecord) -> KnowledgeEvidenceResponse:
    evidence = record.evidence
    deep_link = (
        f"/sources/{record.source_id}?versionId={evidence.source_version_id}"
        f"&artifactId={evidence.parse_artifact_id}&sectionId={evidence.section_id}"
    )
    return KnowledgeEvidenceResponse(
        id=evidence.id,
        node_id=record.node_id,
        revision_id=evidence.revision_id,
        revision_number=record.revision_number,
        source_id=record.source_id,
        source_title=record.source_title,
        source_version_id=evidence.source_version_id,
        source_version_number=record.source_version_number,
        source_content_hash=record.source_content_hash,
        parse_artifact_id=evidence.parse_artifact_id,
        artifact_revision=record.artifact_revision,
        section_id=evidence.section_id,
        section_ordinal=record.section_ordinal,
        quote_hash=evidence.quote_hash,
        content_hash=evidence.content_hash,
        locator=dict(evidence.locator),
        frozen_quote=evidence.frozen_quote,
        deep_link=deep_link,
        created_at=evidence.created_at,
    )


def _write_response(outcome: KnowledgeWriteOutcome) -> KnowledgeWriteResponse:
    return KnowledgeWriteResponse(
        result_id=outcome.result_id,
        request_id=outcome.request_id,
        node_id=outcome.node_id,
        node_version=outcome.node_version,
        snapshot=outcome.snapshot,
    )


@router.get(
    "/knowledge-tree",
    response_model=KnowledgeTreeResponse,
    responses={422: {"model": ErrorEnvelope}},
    operation_id="get_knowledge_tree",
)
async def get_knowledge_tree(
    space_id: UUID, session: SessionDependency
) -> KnowledgeTreeResponse:
    records = await KnowledgeTreeService().list_tree(session, space_id=space_id)
    return KnowledgeTreeResponse(items=[_node_response(record) for record in records])


@router.get(
    "/knowledge-nodes/{node_id}",
    response_model=KnowledgeNodeDetailResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="get_knowledge_node",
)
async def get_knowledge_node(
    space_id: UUID, node_id: UUID, session: SessionDependency
) -> KnowledgeNodeDetailResponse:
    detail = await KnowledgeTreeService().get_node(
        session, space_id=space_id, node_id=node_id
    )
    return KnowledgeNodeDetailResponse(
        node=_node_response(detail),
        revision=_revision_response(detail),
        evidence=[_evidence_response(item) for item in detail.evidence],
    )


@router.get(
    "/knowledge-search",
    response_model=KnowledgeSearchResponse,
    responses={422: {"model": ErrorEnvelope}},
    operation_id="search_knowledge_tree",
)
async def search_knowledge_tree(
    space_id: UUID,
    session: SessionDependency,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> KnowledgeSearchResponse:
    records = await KnowledgeTreeService().search(
        session, space_id=space_id, query=q, limit=limit
    )
    return KnowledgeSearchResponse(items=[_search_response(record) for record in records])


@router.post(
    "/knowledge-nodes",
    response_model=KnowledgeWriteResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
    operation_id="create_knowledge_node",
)
async def create_knowledge_node(
    space_id: UUID,
    request: KnowledgeNodeCreate,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> KnowledgeWriteResponse:
    async with session.begin():
        outcome = await KnowledgeTreeService().create(
            session,
            space_id=space_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _write_response(outcome)


@router.patch(
    "/knowledge-nodes/{node_id}",
    response_model=KnowledgeWriteResponse,
    responses=ERROR_RESPONSES,
    operation_id="edit_knowledge_node",
)
async def edit_knowledge_node(
    space_id: UUID,
    node_id: UUID,
    request: KnowledgeNodeEdit,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> KnowledgeWriteResponse:
    async with session.begin():
        outcome = await KnowledgeTreeService().edit(
            session,
            space_id=space_id,
            node_id=node_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _write_response(outcome)


@router.post(
    "/knowledge-nodes/{node_id}/move",
    response_model=KnowledgeWriteResponse,
    responses=ERROR_RESPONSES,
    operation_id="move_knowledge_node",
)
async def move_knowledge_node(
    space_id: UUID,
    node_id: UUID,
    request: KnowledgeNodeMove,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> KnowledgeWriteResponse:
    async with session.begin():
        outcome = await KnowledgeTreeService().move(
            session,
            space_id=space_id,
            node_id=node_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _write_response(outcome)


@router.delete(
    "/knowledge-nodes/{node_id}",
    response_model=KnowledgeWriteResponse,
    responses=ERROR_RESPONSES,
    operation_id="delete_knowledge_node",
)
async def delete_knowledge_node(
    space_id: UUID,
    node_id: UUID,
    request: KnowledgeNodeDelete,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
) -> KnowledgeWriteResponse:
    async with session.begin():
        outcome = await KnowledgeTreeService().delete(
            session,
            space_id=space_id,
            node_id=node_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    return _write_response(outcome)


@router.get(
    "/knowledge-write-requests/{idempotency_key}",
    response_model=KnowledgeWriteRequestResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="get_knowledge_write_request",
)
async def get_knowledge_write_request(
    space_id: UUID,
    idempotency_key: str,
    session: SessionDependency,
) -> KnowledgeWriteRequestResponse:
    request_row, outcome = await KnowledgeTreeService().get_write_request(
        session, space_id=space_id, idempotency_key=idempotency_key
    )
    return KnowledgeWriteRequestResponse(
        idempotency_key=request_row.idempotency_key,
        operation=request_row.operation,
        result=_write_response(outcome),
    )


@router.get(
    "/knowledge-evidence/{evidence_id}",
    response_model=KnowledgeEvidenceResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="get_knowledge_evidence",
)
async def get_knowledge_evidence(
    space_id: UUID, evidence_id: UUID, session: SessionDependency
) -> KnowledgeEvidenceResponse:
    record = await KnowledgeTreeService().get_evidence(
        session, space_id=space_id, evidence_id=evidence_id
    )
    return _evidence_response(record)
