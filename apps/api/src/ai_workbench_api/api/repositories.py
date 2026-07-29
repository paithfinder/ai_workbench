"""Local repository authorization, scan, list, and revocation endpoints."""

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ai_workbench_api.api.dependencies import (
    get_db_session,
    get_path_validator,
    get_preview_store,
    get_scan_limits,
    get_scan_locks,
    get_scanner,
)
from ai_workbench_api.api.repository_schemas import (
    AuthorizationPreviewResponse,
    AuthorizationResponse,
    LocalAuthorizationPreviewRequest,
    LocalAuthorizationRequest,
    PersonalSpaceResponse,
    RepositoryListResponse,
    RepositoryStatsResponse,
    RepositorySummaryResponse,
    RevocationRequestBody,
    RevocationResponse,
    ScanRequestBody,
    ScanResponse,
    ScanStatsResponse,
)
from ai_workbench_api.api.schemas import ApiEnvelope
from ai_workbench_api.domain.repositories import (
    PERSONAL_SPACE_NAME,
    AuditContext,
    RepositorySummaryData,
    ScanLockRegistry,
)
from ai_workbench_api.security.authorization_previews import AuthorizationPreviewStore
from ai_workbench_api.security.repository_paths import RepositoryPathValidator
from ai_workbench_api.services.repositories import RepositoryService
from ai_workbench_api.sources.local_repository_scanner import LocalRepositoryScanner, ScanLimits

router = APIRouter(
    prefix="/repositories",
    tags=["repositories"],
    responses={
        400: {"model": ApiEnvelope[object], "description": "Invalid repository request"},
        404: {"model": ApiEnvelope[object], "description": "Repository not found"},
        409: {"model": ApiEnvelope[object], "description": "Repository state conflict"},
        413: {"model": ApiEnvelope[object], "description": "Secure scan limit exceeded"},
        422: {"model": ApiEnvelope[object], "description": "Request validation failed"},
        500: {"model": ApiEnvelope[object], "description": "Internal server error"},
    },
)
SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
PathValidatorDep = Annotated[RepositoryPathValidator, Depends(get_path_validator)]
PreviewStoreDep = Annotated[AuthorizationPreviewStore, Depends(get_preview_store)]
ScannerDep = Annotated[LocalRepositoryScanner, Depends(get_scanner)]
ScanLocksDep = Annotated[ScanLockRegistry, Depends(get_scan_locks)]
ScanLimitsDep = Annotated[ScanLimits, Depends(get_scan_limits)]


def _service(
    session: SessionDep,
    validator: PathValidatorDep,
    previews: PreviewStoreDep,
    scanner: ScannerDep,
    locks: ScanLocksDep,
    limits: ScanLimitsDep,
) -> RepositoryService:
    return RepositoryService(
        session,
        path_validator=validator,
        preview_store=previews,
        scanner=scanner,
        scan_locks=locks,
        scan_limits=limits,
    )

ServiceDep = Annotated[RepositoryService, Depends(_service)]


def _request_id(request: Request) -> str:
    return cast(str, request.state.request_id)


def _audit_context(request: Request) -> AuditContext:
    return AuditContext(
        request_id=_request_id(request),
        correlation_id=cast(str, request.state.correlation_id),
    )


def _summary(data: RepositorySummaryData) -> RepositorySummaryResponse:
    return RepositorySummaryResponse(
        id=data.id,
        name=data.name,
        canonical_path=data.canonical_root_path,
        authorization_status=data.authorization_status,
        authorization_epoch=data.authorization_epoch,
        authorized_at=data.authorized_at,
        revoked_at=data.revoked_at,
        scan_state=data.scan_state,
        manifest_hash=data.manifest_hash,
        stats=RepositoryStatsResponse(
            eligible_files=data.eligible_files, eligible_bytes=data.eligible_bytes
        ),
        indexing_state=data.indexing_state,
    )


@router.get("", response_model=ApiEnvelope[RepositoryListResponse])
async def list_repositories(
    request: Request, service: ServiceDep
) -> ApiEnvelope[RepositoryListResponse]:
    space_id, repositories = await service.list_repositories()
    return ApiEnvelope(
        data=RepositoryListResponse(
            space=PersonalSpaceResponse(
                id=space_id, name=PERSONAL_SPACE_NAME, slug="personal-development"
            ),
            repositories=[_summary(item) for item in repositories],
        ),
        request_id=_request_id(request),
    )


@router.post(
    "/local/authorization-previews",
    response_model=ApiEnvelope[AuthorizationPreviewResponse],
)
async def create_authorization_preview(
    body: LocalAuthorizationPreviewRequest, request: Request, service: ServiceDep
) -> ApiEnvelope[AuthorizationPreviewResponse]:
    preview = service.preview(body.path)
    return ApiEnvelope(
        data=AuthorizationPreviewResponse(
            state="ready",
            preview_token=preview.token,
            expires_at=preview.expires_at,
            display_name=preview.candidate.display_name,
            canonical_path=preview.candidate.canonical_path,
            policy_version=preview.candidate.policy_version,
            security_notice=(
                "Read-only access is limited to this root. Links, credentials, secrets, "
                "binary files, and oversized content are excluded."
            ),
        ),
        request_id=_request_id(request),
    )


@router.post(
    "/local/authorizations", response_model=ApiEnvelope[AuthorizationResponse]
)
async def authorize_local_repository(
    body: LocalAuthorizationRequest, request: Request, service: ServiceDep
) -> ApiEnvelope[AuthorizationResponse]:
    result = await service.authorize(body.preview_token, _audit_context(request))
    return ApiEnvelope(
        data=AuthorizationResponse(
            outcome=cast(
                Literal["authorized", "already_authorized", "reauthorized"], result.outcome
            ),
            repository=_summary(result.repository),
        ),
        request_id=_request_id(request),
    )


@router.post("/{repository_id}/scans", response_model=ApiEnvelope[ScanResponse])
async def scan_repository(
    repository_id: UUID, body: ScanRequestBody, request: Request, service: ServiceDep
) -> ApiEnvelope[ScanResponse]:
    result = await service.scan(
        repository_id, body.expected_authorization_epoch, _audit_context(request)
    )
    return ApiEnvelope(
        data=ScanResponse(
            outcome=cast(Literal["created", "unchanged"], result.outcome),
            manifest_hash=result.result.manifest_hash,
            source_version_id=result.version_id,
            index_job_id=result.job_id,
            indexing_state=cast(Literal["not_queued", "pending"], result.indexing_state),
            stats=ScanStatsResponse(
                directories_visited=result.result.stats.directories_visited,
                files_seen=result.result.stats.files_seen,
                eligible_files=result.result.stats.eligible_files,
                eligible_bytes=result.result.stats.eligible_bytes,
                skipped=dict(result.result.stats.skipped),
            ),
        ),
        request_id=_request_id(request),
    )


@router.post("/{repository_id}/revocation", response_model=ApiEnvelope[RevocationResponse])
async def revoke_repository(
    repository_id: UUID, body: RevocationRequestBody, request: Request, service: ServiceDep
) -> ApiEnvelope[RevocationResponse]:
    result = await service.revoke(
        repository_id, body.expected_authorization_epoch, _audit_context(request)
    )
    return ApiEnvelope(
        data=RevocationResponse(
            outcome=cast(Literal["revoked", "already_revoked"], result.outcome),
            repository=_summary(result.repository),
        ),
        request_id=_request_id(request),
    )
