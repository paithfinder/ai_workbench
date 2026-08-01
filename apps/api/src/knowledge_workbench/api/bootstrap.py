from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.api.dependencies import SessionDependency
from knowledge_workbench.db.models import ActivityEvent, Job, KnowledgeSpace, Source

router = APIRouter(prefix="/api/v1", tags=["bootstrap"])

DEFAULT_SPACE_SLUG = "my-knowledge-base"


class KnowledgeSpaceResponse(BaseModel):
    id: UUID
    slug: str
    name: str


class CapabilityResponse(BaseModel):
    source_import: bool = True
    extraction_review: bool = False
    knowledge_tree: bool = False
    trusted_qa: bool = False
    spaced_review: bool = False
    evidence_agent: bool = False


class BootstrapStatistics(BaseModel):
    sources: int
    queued_jobs: int
    activity_events: int


class BootstrapResponse(BaseModel):
    space: KnowledgeSpaceResponse
    capabilities: CapabilityResponse
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
async def bootstrap(session: SessionDependency) -> BootstrapResponse:
    space = await _load_default_space(session)
    source_count = await session.scalar(
        select(func.count()).select_from(Source).where(Source.space_id == space.id)
    )
    job_count = await session.scalar(
        select(func.count()).select_from(Job).where(
            Job.space_id == space.id, Job.status.in_(("queued", "running"))
        )
    )
    event_count = await session.scalar(
        select(func.count()).select_from(ActivityEvent).where(ActivityEvent.space_id == space.id)
    )
    return BootstrapResponse(
        space=KnowledgeSpaceResponse(id=space.id, slug=space.slug, name=space.name),
        capabilities=CapabilityResponse(),
        statistics=BootstrapStatistics(
            sources=source_count or 0,
            queued_jobs=job_count or 0,
            activity_events=event_count or 0,
        ),
    )
