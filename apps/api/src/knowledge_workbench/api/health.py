from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from minio import Minio
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class DependencyStatus(BaseModel):
    database: Literal["ok", "unavailable"]
    redis: Literal["ok", "unavailable"]
    object_storage: Literal["ok", "unavailable"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    dependencies: DependencyStatus


@router.get("/live", response_model=LivenessResponse, operation_id="get_liveness")
async def live(request: Request) -> LivenessResponse:
    settings = request.app.state.settings
    return LivenessResponse(status="ok", service=settings.app_name, version=settings.app_version)


async def _database_status(engine: AsyncEngine) -> Literal["ok", "unavailable"]:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return "unavailable"
    return "ok"


async def _redis_status(redis: Redis) -> Literal["ok", "unavailable"]:
    try:
        if not await redis.ping():
            return "unavailable"
    except Exception:
        return "unavailable"
    return "ok"


async def _storage_status(client: Minio, bucket: str) -> Literal["ok", "unavailable"]:
    try:
        exists = await asyncio.to_thread(client.bucket_exists, bucket)
    except Exception:
        return "unavailable"
    return "ok" if exists else "unavailable"


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
    operation_id="get_readiness",
)
async def ready(request: Request) -> ReadinessResponse | JSONResponse:
    database, redis, storage = await asyncio.gather(
        _database_status(request.app.state.engine),
        _redis_status(request.app.state.redis),
        _storage_status(request.app.state.storage, request.app.state.settings.s3_bucket),
    )
    dependencies = DependencyStatus(
        database=database,
        redis=redis,
        object_storage=storage,
    )
    ready_status = all(value == "ok" for value in dependencies.model_dump().values())
    response = ReadinessResponse(
        status="ready" if ready_status else "not_ready",
        dependencies=dependencies,
    )
    if not ready_status:
        return JSONResponse(status_code=503, content=response.model_dump(mode="json"))
    return response
