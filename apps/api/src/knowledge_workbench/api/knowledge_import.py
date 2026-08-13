from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import BaseModel

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.application.indexing import IndexingService
from knowledge_workbench.application.knowledge_import import (
    KnowledgeImportCreate,
    KnowledgeImportOutcome,
    KnowledgeImportService,
)
from knowledge_workbench.core.errors import ErrorEnvelope

router = APIRouter(
    prefix="/api/v1/knowledge-spaces/{space_id}", tags=["knowledge-import"]
)
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
]
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (404, 409, 413, 422)
}


class KnowledgeImportSummaryResponse(BaseModel):
    entry_count: int
    folder_count: int
    document_count: int
    total_body_utf8_bytes: int


class KnowledgeImportItemResponse(BaseModel):
    ordinal: int
    relative_path: str
    kind: Literal["folder", "document"]
    node_id: UUID
    revision_id: UUID
    content_hash: str


class KnowledgeImportResponse(BaseModel):
    request_id: UUID
    idempotency_key: str
    status: Literal["succeeded"] = "succeeded"
    root_node_id: UUID
    summary: KnowledgeImportSummaryResponse
    items: list[KnowledgeImportItemResponse]


def _response(outcome: KnowledgeImportOutcome) -> KnowledgeImportResponse:
    return KnowledgeImportResponse(
        request_id=outcome.request_id,
        idempotency_key=outcome.idempotency_key,
        root_node_id=outcome.root_node_id,
        summary=KnowledgeImportSummaryResponse(
            entry_count=outcome.entry_count,
            folder_count=outcome.folder_count,
            document_count=outcome.document_count,
            total_body_utf8_bytes=outcome.total_body_utf8_bytes,
        ),
        items=[
            KnowledgeImportItemResponse(
                ordinal=item.ordinal,
                relative_path=item.relative_path,
                kind=item.kind,  # type: ignore[arg-type]
                node_id=item.node_id,
                revision_id=item.revision_id,
                content_hash=item.content_hash,
            )
            for item in outcome.items
        ],
    )


@router.post(
    "/knowledge-imports",
    response_model=KnowledgeImportResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
    operation_id="create_knowledge_import",
)
async def create_knowledge_import(
    space_id: UUID,
    request: KnowledgeImportCreate,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    settings: SettingsDependency,
) -> KnowledgeImportResponse:
    async with session.begin():
        outcome = await KnowledgeImportService().create(
            session,
            space_id=space_id,
            idempotency_key=idempotency_key,
            request=request,
            settings=settings,
        )
        for item in outcome.items:
            if item.kind != "document":
                continue
            await IndexingService().request_knowledge_rebuild(
                session,
                settings=settings,
                space_id=space_id,
                revision_id=item.revision_id,
                idempotency_key=f"knowledge-import:{outcome.request_id}:{item.ordinal}",
            )
    return _response(outcome)


@router.get(
    "/knowledge-import-requests/{idempotency_key}",
    response_model=KnowledgeImportResponse,
    responses={404: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
    operation_id="get_knowledge_import_request",
)
async def get_knowledge_import_request(
    space_id: UUID,
    idempotency_key: str,
    session: SessionDependency,
) -> KnowledgeImportResponse:
    outcome = await KnowledgeImportService().get_request(
        session,
        space_id=space_id,
        idempotency_key=idempotency_key,
    )
    return _response(outcome)
