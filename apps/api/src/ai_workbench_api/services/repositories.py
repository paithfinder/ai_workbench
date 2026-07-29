"""Transactional orchestration for secure local repository lifecycle operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_workbench_api.api.schemas import ApiError
from ai_workbench_api.db.repository_store import RepositoryStore, utcnow
from ai_workbench_api.domain.repositories import (
    AuditContext,
    RepositorySnapshot,
    RepositorySummaryData,
    ScanLockRegistry,
)
from ai_workbench_api.security.authorization_previews import (
    AuthorizationPreviewError,
    AuthorizationPreviewStore,
)
from ai_workbench_api.security.repository_paths import (
    RepositoryPathError,
    RepositoryPathValidator,
    RootIdentity,
)
from ai_workbench_api.sources.local_repository_scanner import (
    LocalRepositoryScanner,
    ScanError,
    ScanLimits,
    ScanRequest,
    ScanResult,
)


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    outcome: str
    repository: RepositorySummaryData


@dataclass(frozen=True, slots=True)
class ScanServiceResult:
    outcome: str
    result: ScanResult
    version_id: uuid.UUID
    job_id: uuid.UUID | None
    indexing_state: str


@dataclass(frozen=True, slots=True)
class RevocationResult:
    outcome: str
    repository: RepositorySummaryData


class RepositoryService:
    """Keep filesystem work outside transactions and enforce epoch rechecks."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        path_validator: RepositoryPathValidator,
        preview_store: AuthorizationPreviewStore,
        scanner: LocalRepositoryScanner,
        scan_locks: ScanLockRegistry,
        scan_limits: ScanLimits | None = None,
    ) -> None:
        self.session = session
        self.store = RepositoryStore(session)
        self.path_validator = path_validator
        self.preview_store = preview_store
        self.scanner = scanner
        self.scan_locks = scan_locks
        self.scan_limits = scan_limits or ScanLimits()

    def preview(self, raw_path: str):  # type: ignore[no-untyped-def]
        try:
            candidate = self.path_validator.validate(raw_path)
            return self.preview_store.create(candidate)
        except RepositoryPathError as exc:
            raise _safe_error(exc.code) from exc

    async def authorize(self, token: str, context: AuditContext) -> AuthorizationResult:
        try:
            candidate = self.preview_store.consume(token)
            candidate = self.path_validator.revalidate(candidate)
        except (AuthorizationPreviewError, RepositoryPathError) as exc:
            raise _safe_error(exc.code) from exc

        for attempt in range(2):
            try:
                space = await self.store.ensure_personal_space()
                repository = await self.store.repository_by_root_key(
                    space.id, candidate.normalized_root_key, for_update=True
                )
                now = utcnow()
                if repository is None:
                    repository, _ = await self.store.create_authorized_repository(
                        space, candidate, now
                    )
                    outcome = "authorized"
                    action = "repository.authorized"
                elif repository.authorization_status == "authorized":
                    if repository.root_identity != candidate.root_identity.value:
                        raise _safe_error("authorization_candidate_changed")
                    outcome = "already_authorized"
                    action = "repository.authorization_reused"
                else:
                    repository.authorization_status = "authorized"
                    repository.authorization_epoch += 1
                    repository.authorized_at = now
                    repository.revoked_at = None
                    repository.canonical_root_path = candidate.canonical_path
                    repository.normalized_root_key = candidate.normalized_root_key
                    repository.root_identity = candidate.root_identity.value
                    repository.policy_version = candidate.policy_version
                    repository.name = candidate.display_name
                    source = await self.store.source_for_repository(repository.id, for_update=True)
                    if source is None:
                        source = await self.store.create_source(space.id, repository)
                    source.status = "active"
                    source.title = repository.name
                    outcome = "reauthorized"
                    action = "repository.reauthorized"
                self.store.add_audit(
                    space_id=space.id,
                    repository_id=repository.id,
                    action=action,
                    context=context,
                    payload={
                        "outcome": outcome,
                        "authorization_epoch": repository.authorization_epoch,
                        "policy_version": candidate.policy_version,
                    },
                )
                await self.session.commit()
                return AuthorizationResult(
                    outcome=outcome,
                    repository=await self._summary(repository.id),
                )
            except IntegrityError:
                await self.session.rollback()
                if attempt == 1:
                    raise _safe_error("authorization_conflict") from None
        raise AssertionError("unreachable")

    async def list_repositories(self) -> tuple[uuid.UUID, list[RepositorySummaryData]]:
        space = await self.store.ensure_personal_space()
        await self.session.commit()
        return space.id, await self.store.summaries(space.id)

    async def scan(
        self, repository_id: uuid.UUID, expected_epoch: int, context: AuditContext
    ) -> ScanServiceResult:
        if not await self.scan_locks.acquire(repository_id):
            raise _safe_error("scan_in_progress")
        try:
            snapshot = await self.store.snapshot(repository_id)
            if snapshot is None:
                raise _safe_error("repository_not_authorized")
            if snapshot.authorization_epoch != expected_epoch:
                raise _safe_error("stale_authorization_epoch")
            await self.session.rollback()
            try:
                result = await self.scanner.scan(
                    ScanRequest(
                        root_path=snapshot.canonical_root_path,
                        normalized_root_key=snapshot.normalized_root_key,
                        root_identity=RootIdentity(snapshot.root_identity),
                        policy_version=snapshot.policy_version,
                        limits=self.scan_limits,
                    )
                )
            except ScanError as exc:
                if exc.code == "repository_root_changed":
                    revoked = await self._revoke_changed_root(snapshot, context)
                    if not revoked:
                        raise _safe_error("stale_authorization_epoch") from exc
                raise _safe_error(exc.code) from exc
            return await self._persist_scan(snapshot, result, context)
        finally:
            await self.scan_locks.release(repository_id)

    async def revoke(
        self, repository_id: uuid.UUID, expected_epoch: int, context: AuditContext
    ) -> RevocationResult:
        repository = await self.store.repository(repository_id, for_update=True)
        if repository is None:
            raise _safe_error("repository_not_found")
        if repository.authorization_epoch != expected_epoch:
            raise _safe_error("stale_authorization_epoch")
        if repository.authorization_status == "revoked":
            outcome = "already_revoked"
        else:
            now = utcnow()
            repository.authorization_status = "revoked"
            repository.revoked_at = now
            repository.authorization_epoch += 1
            source = await self.store.source_for_repository(repository.id, for_update=True)
            if source is not None:
                source.status = "disabled"
            await self.store.cancel_pending_jobs(repository.id, now)
            self.store.add_audit(
                space_id=repository.space_id,
                repository_id=repository.id,
                action="repository.revoked",
                context=context,
                payload={
                    "outcome": "revoked",
                    "authorization_epoch": repository.authorization_epoch,
                    "reason": "user_requested",
                },
            )
            outcome = "revoked"
        await self.session.commit()
        return RevocationResult(outcome=outcome, repository=await self._summary(repository.id))

    async def _persist_scan(
        self, snapshot: RepositorySnapshot, result: ScanResult, context: AuditContext
    ) -> ScanServiceResult:
        repository = await self.store.repository(snapshot.id, for_update=True)
        source = await self.store.source_for_repository(snapshot.id, for_update=True)
        if (
            repository is None
            or source is None
            or repository.authorization_status != "authorized"
            or source.status != "active"
            or repository.authorization_epoch != snapshot.authorization_epoch
            or repository.root_identity != snapshot.root_identity
            or repository.normalized_root_key != snapshot.normalized_root_key
            or repository.policy_version != snapshot.policy_version
        ):
            await self.session.rollback()
            raise _safe_error("stale_authorization_epoch")
        version, created = await self.store.create_or_get_version(snapshot, result)
        job = await self.store.ensure_pending_job(snapshot.space_id, version)
        self.store.add_audit(
            space_id=snapshot.space_id,
            repository_id=snapshot.id,
            action="repository.scanned",
            context=context,
            payload={
                "outcome": "created" if created else "unchanged",
                "authorization_epoch": snapshot.authorization_epoch,
                "policy_version": snapshot.policy_version,
                "manifest_hash": result.manifest_hash,
                "directories_visited": result.stats.directories_visited,
                "files_seen": result.stats.files_seen,
                "eligible_files": result.stats.eligible_files,
                "eligible_bytes": result.stats.eligible_bytes,
                "skipped": dict(result.stats.skipped),
            },
        )
        await self.session.commit()
        return ScanServiceResult(
            outcome="created" if created else "unchanged",
            result=result,
            version_id=version.id,
            job_id=job.id if job is not None else None,
            indexing_state=job.status if job is not None else "not_queued",
        )

    async def _revoke_changed_root(
        self, snapshot: RepositorySnapshot, context: AuditContext
    ) -> bool:
        repository = await self.store.repository(snapshot.id, for_update=True)
        if (
            repository is None
            or repository.authorization_status != "authorized"
            or repository.authorization_epoch != snapshot.authorization_epoch
        ):
            await self.session.rollback()
            return False
        now = utcnow()
        repository.authorization_status = "revoked"
        repository.revoked_at = now
        repository.authorization_epoch += 1
        source = await self.store.source_for_repository(repository.id, for_update=True)
        if source is not None:
            source.status = "disabled"
        await self.store.cancel_pending_jobs(repository.id, now)
        self.store.add_audit(
            space_id=repository.space_id,
            repository_id=repository.id,
            action="repository.auto_revoked",
            context=context,
            payload={
                "outcome": "revoked",
                "authorization_epoch": repository.authorization_epoch,
                "reason": "repository_root_changed",
            },
        )
        await self.session.commit()
        return True

    async def _summary(self, repository_id: uuid.UUID) -> RepositorySummaryData:
        repository = await self.store.repository(repository_id)
        if repository is None:
            raise RuntimeError("repository disappeared after commit")
        summaries = await self.store.summaries(repository.space_id)
        return next(item for item in summaries if item.id == repository_id)


def _safe_error(code: str) -> ApiError:
    statuses = {
        "invalid_local_path": 400,
        "network_path_denied": 400,
        "authorization_preview_expired": 409,
        "authorization_candidate_changed": 409,
        "authorization_conflict": 409,
        "repository_not_authorized": 409,
        "stale_authorization_epoch": 409,
        "repository_root_changed": 409,
        "scan_in_progress": 409,
        "scan_limit_exceeded": 413,
        "scan_failed_closed": 409,
        "repository_not_found": 404,
    }
    messages = {
        "invalid_local_path": "The local repository path is invalid",
        "network_path_denied": "Network repository paths are not allowed",
        "authorization_preview_expired": "The authorization preview expired or was already used",
        "authorization_candidate_changed": "The repository changed after preview",
        "authorization_conflict": "The repository authorization conflicted with another request",
        "repository_not_authorized": "The repository is not authorized",
        "stale_authorization_epoch": "The repository authorization changed; refresh and retry",
        "repository_root_changed": "The repository root changed and authorization was revoked",
        "scan_in_progress": "A scan is already in progress for this repository",
        "scan_limit_exceeded": "The repository exceeds secure scan limits",
        "scan_failed_closed": "The repository scan could not be completed safely",
        "repository_not_found": "Repository not found",
    }
    return ApiError(statuses.get(code, 400), code, messages.get(code, "Repository request failed"))
