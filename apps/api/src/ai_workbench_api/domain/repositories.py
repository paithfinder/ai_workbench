"""Framework-independent repository states, DTOs, and scan lock coordination."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

POLICY_VERSION = "local-repository-v1"
PERSONAL_SPACE_SLUG = "personal-development"
PERSONAL_SPACE_NAME = "Personal Development"

AuthorizationStatus = Literal["pending", "authorized", "revoked"]
ScanState = Literal["not_scanned", "manifest_ready"]
IndexingState = Literal[
    "not_queued", "pending", "running", "succeeded", "failed", "cancelled"
]


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Non-sensitive request identifiers attached to an audit event."""

    request_id: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Authorized repository state captured before filesystem work."""

    id: uuid.UUID
    space_id: uuid.UUID
    source_id: uuid.UUID
    name: str
    canonical_root_path: str
    normalized_root_key: str
    root_identity: str
    authorization_epoch: int
    policy_version: str


@dataclass(frozen=True, slots=True)
class RepositorySummaryData:
    """Persistence-neutral repository summary returned by the service."""

    id: uuid.UUID
    name: str
    canonical_root_path: str
    authorization_status: AuthorizationStatus
    authorization_epoch: int
    authorized_at: datetime | None
    revoked_at: datetime | None
    scan_state: ScanState
    manifest_hash: str | None
    eligible_files: int
    eligible_bytes: int
    indexing_state: IndexingState


class ScanLockRegistry:
    """Process-local non-blocking lock registry keyed by repository ID."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._active: set[uuid.UUID] = set()

    async def acquire(self, repository_id: uuid.UUID) -> bool:
        """Acquire a repository slot without waiting for an existing scan."""
        async with self._guard:
            if repository_id in self._active:
                return False
            self._active.add(repository_id)
            return True

    async def release(self, repository_id: uuid.UUID) -> None:
        """Release a previously acquired repository slot."""
        async with self._guard:
            self._active.discard(repository_id)
