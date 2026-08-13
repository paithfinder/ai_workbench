from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.api.dependencies import SessionDependency, SettingsDependency
from knowledge_workbench.db.models import ActivityEvent, Job, KnowledgeSpace, Source

router = APIRouter(prefix="/api/v1", tags=["bootstrap"])

DEFAULT_SPACE_SLUG = "my-knowledge-base"


class KnowledgeSpaceResponse(BaseModel):
    id: UUID
    slug: str
    name: str


class CapabilityResponse(BaseModel):
    source_import: bool = True
    extraction_review: bool = True
    knowledge_tree: bool = True
    knowledge_folder_import: bool = True
    retrieval_debug: bool = True
    trusted_qa: bool = True
    spaced_review: bool = False
    evidence_agent: bool = False


class KnowledgeImportLimitsResponse(BaseModel):
    max_entries: int
    max_folders: int
    max_depth: int
    max_total_body_utf8_bytes: int
    max_document_characters: int
    max_relative_path_characters: int
    allowed_extensions: list[str] = [".md", ".txt"]


class BootstrapLimitsResponse(BaseModel):
    knowledge_import: KnowledgeImportLimitsResponse


class BootstrapStatistics(BaseModel):
    sources: int
    queued_jobs: int
    activity_events: int


class BootstrapResponse(BaseModel):
    space: KnowledgeSpaceResponse
    capabilities: CapabilityResponse
    limits: BootstrapLimitsResponse
    statistics: BootstrapStatistics
    foundation_status: Literal["ready"] = "ready"


async def _load_default_space(session: AsyncSession) -> KnowledgeSpace:
    result = await session.execute(
        select(KnowledgeSpace).where(KnowledgeSpace.slug == DEFAULT_SPACE_SLUG)
    )
    space = result.scalar_one()
    return space


@router.get(
    "/knowledge-spaces/default",
    response_model=KnowledgeSpaceResponse,
    operation_id="get_default_knowledge_space",
)
async def default_space(session: SessionDependency) -> KnowledgeSpaceResponse:
    space = await _load_default_space(session)
    return KnowledgeSpaceResponse(id=space.id, slug=space.slug, name=space.name)


@router.get("/bootstrap", response_model=BootstrapResponse, operation_id="get_bootstrap")
async def bootstrap(
    session: SessionDependency, settings: SettingsDependency
) -> BootstrapResponse:
    space = await _load_default_space(session)
    source_count = await session.scalar(
        select(func.count()).select_from(Source).where(Source.space_id == space.id)
    )
    job_count = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.space_id == space.id, Job.status.in_(("queued", "running")))
    )
    event_count = await session.scalar(
        select(func.count()).select_from(ActivityEvent).where(ActivityEvent.space_id == space.id)
    )
    return BootstrapResponse(
        space=KnowledgeSpaceResponse(id=space.id, slug=space.slug, name=space.name),
        capabilities=CapabilityResponse(
            retrieval_debug=settings.app_env in {"development", "test"}
        ),
        limits=BootstrapLimitsResponse(
            knowledge_import=KnowledgeImportLimitsResponse(
                max_entries=settings.knowledge_import_max_entries,
                max_folders=settings.knowledge_import_max_folders,
                max_depth=settings.knowledge_import_max_depth,
                max_total_body_utf8_bytes=settings.knowledge_import_max_total_body_bytes,
                max_document_characters=settings.knowledge_import_max_document_characters,
                max_relative_path_characters=settings.knowledge_import_max_relative_path_characters,
            )
        ),
        statistics=BootstrapStatistics(
            sources=source_count or 0,
            queued_jobs=job_count or 0,
            activity_events=event_count or 0,
        ),
    )
