"""Typed request and response contracts for repository lifecycle APIs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalAuthorizationPreviewRequest(StrictModel):
    path: str = Field(min_length=1, max_length=32767)


class AuthorizationPreviewResponse(StrictModel):
    state: Literal["ready"]
    preview_token: str
    expires_at: datetime
    display_name: str
    canonical_path: str
    policy_version: str
    security_notice: str


class LocalAuthorizationRequest(StrictModel):
    preview_token: str = Field(min_length=1, max_length=256)
    confirmation: Literal[True]


class ScanRequestBody(StrictModel):
    expected_authorization_epoch: int = Field(ge=1)


class RevocationRequestBody(StrictModel):
    expected_authorization_epoch: int = Field(ge=1)


class RepositoryStatsResponse(StrictModel):
    eligible_files: int = Field(ge=0)
    eligible_bytes: int = Field(ge=0)


class RepositorySummaryResponse(StrictModel):
    id: UUID
    name: str
    canonical_path: str
    authorization_status: Literal["pending", "authorized", "revoked"]
    authorization_epoch: int = Field(ge=0)
    authorized_at: datetime | None
    revoked_at: datetime | None
    scan_state: Literal["not_scanned", "manifest_ready"]
    manifest_hash: str | None
    stats: RepositoryStatsResponse
    indexing_state: Literal[
        "not_queued", "pending", "running", "succeeded", "failed", "cancelled"
    ]


class PersonalSpaceResponse(StrictModel):
    id: UUID
    name: str
    slug: Literal["personal-development"]


class RepositoryListResponse(StrictModel):
    space: PersonalSpaceResponse
    repositories: list[RepositorySummaryResponse]


class AuthorizationResponse(StrictModel):
    outcome: Literal["authorized", "already_authorized", "reauthorized"]
    repository: RepositorySummaryResponse


class ScanStatsResponse(StrictModel):
    directories_visited: int = Field(ge=0)
    files_seen: int = Field(ge=0)
    eligible_files: int = Field(ge=0)
    eligible_bytes: int = Field(ge=0)
    skipped: dict[str, int]


class ScanResponse(StrictModel):
    outcome: Literal["created", "unchanged"]
    manifest_hash: str
    source_version_id: UUID
    index_job_id: UUID | None
    indexing_state: Literal["not_queued", "pending"]
    stats: ScanStatsResponse


class RevocationResponse(StrictModel):
    outcome: Literal["revoked", "already_revoked"]
    repository: RepositorySummaryResponse
