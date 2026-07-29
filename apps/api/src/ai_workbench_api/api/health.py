"""Health endpoint and readiness contract."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ai_workbench_api.api.schemas import ApiEnvelope

router = APIRouter(tags=["system"])


class HealthData(BaseModel):
    """Non-sensitive service readiness information."""

    app: str
    version: str
    status: Literal["ok", "degraded"]
    database: Literal["connected", "unavailable"]
    model_configured: bool


@router.get("/health", response_model=ApiEnvelope[HealthData])
async def health(request: Request) -> ApiEnvelope[HealthData]:
    """Return application, database, and model configuration readiness."""
    database_connected = bool(getattr(request.app.state, "database_connected", False))
    settings = request.app.state.settings
    return ApiEnvelope(
        data=HealthData(
            app=settings.app_name,
            version=settings.app_version,
            status="ok" if database_connected else "degraded",
            database="connected" if database_connected else "unavailable",
            model_configured=settings.model_configured,
        ),
        request_id=request.state.request_id,
    )
