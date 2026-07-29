"""Focused repository service tests for transaction, epoch, and scan coordination."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_workbench_api.api.schemas import ApiError
from ai_workbench_api.db.models import SourceVersion
from ai_workbench_api.db.repository_store import RepositoryStore
from ai_workbench_api.domain.repositories import (
    AuditContext,
    RepositorySnapshot,
    RepositorySummaryData,
    ScanLockRegistry,
)
from ai_workbench_api.security.authorization_previews import AuthorizationPreviewStore
from ai_workbench_api.security.repository_paths import RepositoryPathCandidate, RootIdentity
from ai_workbench_api.services.repositories import RepositoryService
from ai_workbench_api.sources.local_repository_scanner import (
    LocalRepositoryScanner,
    ScanError,
    ScanResult,
    ScanStats,
)

_CONTEXT = AuditContext(request_id="request-1", correlation_id="correlation-1")


@pytest.mark.asyncio
async def test_scan_lock_registry_is_non_blocking_and_repository_scoped() -> None:
    registry = ScanLockRegistry()
    first = uuid4()
    second = uuid4()

    assert await registry.acquire(first) is True
    assert await registry.acquire(first) is False
    assert await registry.acquire(second) is True

    await registry.release(first)
    assert await registry.acquire(first) is True

    await asyncio.gather(registry.release(first), registry.release(second))


@pytest.mark.asyncio
async def test_authorize_existing_repository_is_idempotent_without_epoch_increment() -> None:
    candidate = _candidate()
    previews = AuthorizationPreviewStore()
    preview = previews.create(candidate)
    validator = MagicMock()
    validator.revalidate.return_value = candidate
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    service = _service(session, validator, previews)
    repository = SimpleNamespace(
        id=uuid4(),
        space_id=uuid4(),
        root_identity=candidate.root_identity.value,
        authorization_status="authorized",
        authorization_epoch=7,
    )
    space = SimpleNamespace(id=repository.space_id)
    summary = _summary(repository.id, repository.space_id, epoch=7)
    store = MagicMock()
    store.ensure_personal_space = AsyncMock(return_value=space)
    store.repository_by_root_key = AsyncMock(return_value=repository)
    store.repository = AsyncMock(return_value=repository)
    store.summaries = AsyncMock(return_value=[summary])
    service.store = store

    result = await service.authorize(preview.token, _CONTEXT)

    assert result.outcome == "already_authorized"
    assert repository.authorization_epoch == 7
    store.create_authorized_repository.assert_not_called()
    store.add_audit.assert_called_once()
    assert store.add_audit.call_args.kwargs["payload"] == {
        "outcome": "already_authorized",
        "authorization_epoch": 7,
        "policy_version": candidate.policy_version,
    }
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reauthorize_increments_epoch_and_reactivates_single_source() -> None:
    candidate = _candidate()
    previews = AuthorizationPreviewStore()
    preview = previews.create(candidate)
    validator = MagicMock()
    validator.revalidate.return_value = candidate
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    service = _service(session, validator, previews)
    repository = SimpleNamespace(
        id=uuid4(),
        space_id=uuid4(),
        root_identity="old-identity",
        authorization_status="revoked",
        authorization_epoch=3,
        authorized_at=None,
        revoked_at=datetime.now(UTC),
        canonical_root_path="old",
        normalized_root_key=candidate.normalized_root_key,
        policy_version=None,
        name="old",
    )
    source = SimpleNamespace(status="disabled", title="old")
    space = SimpleNamespace(id=repository.space_id)
    summary = _summary(repository.id, repository.space_id, epoch=4)
    store = MagicMock()
    store.ensure_personal_space = AsyncMock(return_value=space)
    store.repository_by_root_key = AsyncMock(return_value=repository)
    store.source_for_repository = AsyncMock(return_value=source)
    store.repository = AsyncMock(return_value=repository)
    store.summaries = AsyncMock(return_value=[summary])
    service.store = store

    result = await service.authorize(preview.token, _CONTEXT)

    assert result.outcome == "reauthorized"
    assert repository.authorization_epoch == 4
    assert repository.authorization_status == "authorized"
    assert repository.revoked_at is None
    assert repository.root_identity == candidate.root_identity.value
    assert source.status == "active"
    store.create_source.assert_not_called()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_rejects_stale_epoch_before_filesystem_access() -> None:
    session = MagicMock(spec=AsyncSession)
    session.rollback = AsyncMock()
    scanner = MagicMock(spec=LocalRepositoryScanner)
    scanner.scan = AsyncMock()
    service = RepositoryService(
        session,
        path_validator=MagicMock(),
        preview_store=AuthorizationPreviewStore(),
        scanner=scanner,
        scan_locks=ScanLockRegistry(),
    )
    repository_id = uuid4()
    store = MagicMock()
    store.snapshot = AsyncMock(return_value=_snapshot(repository_id, epoch=5))
    service.store = store

    with pytest.raises(ApiError) as raised:
        await service.scan(repository_id, 4, _CONTEXT)

    assert raised.value.code == "stale_authorization_epoch"
    scanner.scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_rejects_stale_epoch_without_mutation_or_commit() -> None:
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    service = _service(session, MagicMock(), AuthorizationPreviewStore())
    repository = SimpleNamespace(
        id=uuid4(), authorization_epoch=9, authorization_status="authorized"
    )
    store = MagicMock()
    store.repository = AsyncMock(return_value=repository)
    service.store = store

    with pytest.raises(ApiError) as raised:
        await service.revoke(repository.id, 8, _CONTEXT)

    assert raised.value.code == "stale_authorization_epoch"
    assert repository.authorization_status == "authorized"
    store.cancel_pending_jobs.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_scan_created_and_unchanged_preserves_job_idempotency() -> None:
    repository_id = uuid4()
    snapshot = _snapshot(repository_id, epoch=2)
    result = _scan_result(eligible_files=1)
    repository = SimpleNamespace(
        id=repository_id,
        authorization_status="authorized",
        authorization_epoch=2,
        root_identity=snapshot.root_identity,
        normalized_root_key=snapshot.normalized_root_key,
        policy_version=snapshot.policy_version,
    )
    source = SimpleNamespace(id=snapshot.source_id, status="active")
    version = SimpleNamespace(id=uuid4())
    job = SimpleNamespace(id=uuid4(), status="pending")
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    service = _service(session, MagicMock(), AuthorizationPreviewStore())
    store = MagicMock()
    store.repository = AsyncMock(return_value=repository)
    store.source_for_repository = AsyncMock(return_value=source)
    store.create_or_get_version = AsyncMock(
        side_effect=[(version, True), (version, False)]
    )
    store.ensure_pending_job = AsyncMock(return_value=job)
    service.store = store

    created = await service._persist_scan(snapshot, result, _CONTEXT)
    unchanged = await service._persist_scan(snapshot, result, _CONTEXT)

    assert created.outcome == "created"
    assert unchanged.outcome == "unchanged"
    assert created.job_id == unchanged.job_id == job.id
    assert session.commit.await_count == 2


@pytest.mark.asyncio
async def test_store_empty_manifest_does_not_queue_index_job() -> None:
    session = MagicMock(spec=AsyncSession)
    store = RepositoryStore(session)
    version = SourceVersion(
        source_id=uuid4(),
        version_identifier="scan:1:" + "0" * 64,
        content_hash="0" * 64,
        authorization_epoch=1,
        policy_version="local-repository-v1",
        version_metadata={"eligible_files": 0},
    )

    assert await store.ensure_pending_job(uuid4(), version) is None
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_store_next_job_attempt_follows_terminal_attempt() -> None:
    version = SourceVersion(
        id=uuid4(),
        source_id=uuid4(),
        version_identifier="scan:1:" + "1" * 64,
        content_hash="1" * 64,
        authorization_epoch=1,
        policy_version="local-repository-v1",
        version_metadata={"eligible_files": 1},
    )
    active_result = MagicMock()
    active_result.scalars.return_value.first.return_value = None
    max_result = MagicMock()
    max_result.scalar_one.return_value = 3
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[active_result, max_result])
    session.flush = AsyncMock()
    store = RepositoryStore(session)

    job = await store.ensure_pending_job(uuid4(), version)

    assert job is not None
    assert job.attempt == 4
    assert job.status == "pending"
    session.add.assert_called_once_with(job)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_reuses_highest_active_attempt_without_creating_job() -> None:
    version = SourceVersion(
        id=uuid4(),
        source_id=uuid4(),
        version_identifier="scan:1:" + "2" * 64,
        content_hash="2" * 64,
        authorization_epoch=1,
        policy_version="local-repository-v1",
        version_metadata={"eligible_files": 1},
    )
    active = SimpleNamespace(id=uuid4(), status="running", attempt=5)
    active_result = MagicMock()
    active_result.scalars.return_value.first.return_value = active
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=active_result)
    store = RepositoryStore(session)

    job = await store.ensure_pending_job(uuid4(), version)

    assert job is not None
    assert job.id == active.id
    assert job.attempt == 5
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_revoke_disables_source_cancels_jobs_and_increments_epoch() -> None:
    repository = SimpleNamespace(
        id=uuid4(),
        space_id=uuid4(),
        authorization_epoch=4,
        authorization_status="authorized",
        revoked_at=None,
    )
    source = SimpleNamespace(status="active")
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    service = _service(session, MagicMock(), AuthorizationPreviewStore())
    store = MagicMock()
    store.repository = AsyncMock(return_value=repository)
    store.source_for_repository = AsyncMock(return_value=source)
    store.cancel_pending_jobs = AsyncMock()
    store.summaries = AsyncMock(
        return_value=[_summary(repository.id, repository.space_id, epoch=5)]
    )
    service.store = store

    result = await service.revoke(repository.id, 4, _CONTEXT)

    assert result.outcome == "revoked"
    assert repository.authorization_epoch == 5
    assert repository.authorization_status == "revoked"
    assert source.status == "disabled"
    store.cancel_pending_jobs.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_root_replacement_auto_revokes_before_returning_conflict() -> None:
    repository_id = uuid4()
    snapshot = _snapshot(repository_id, epoch=3)
    repository = SimpleNamespace(
        id=repository_id,
        space_id=snapshot.space_id,
        authorization_epoch=3,
        authorization_status="authorized",
        revoked_at=None,
    )
    source = SimpleNamespace(status="active")
    scanner = MagicMock(spec=LocalRepositoryScanner)
    scanner.scan = AsyncMock(side_effect=ScanError("repository_root_changed"))
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    service = RepositoryService(
        session,
        path_validator=MagicMock(),
        preview_store=AuthorizationPreviewStore(),
        scanner=scanner,
        scan_locks=ScanLockRegistry(),
    )
    store = MagicMock()
    store.snapshot = AsyncMock(return_value=snapshot)
    store.repository = AsyncMock(return_value=repository)
    store.source_for_repository = AsyncMock(return_value=source)
    store.cancel_pending_jobs = AsyncMock()
    service.store = store

    with pytest.raises(ApiError) as raised:
        await service.scan(repository_id, 3, _CONTEXT)

    assert raised.value.code == "repository_root_changed"
    assert repository.authorization_status == "revoked"
    assert repository.authorization_epoch == 4
    assert source.status == "disabled"
    store.cancel_pending_jobs.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_root_replacement_after_concurrent_epoch_change_returns_stale() -> None:
    repository_id = uuid4()
    snapshot = _snapshot(repository_id, epoch=3)
    changed = SimpleNamespace(
        id=repository_id,
        space_id=snapshot.space_id,
        authorization_epoch=4,
        authorization_status="authorized",
    )
    scanner = MagicMock(spec=LocalRepositoryScanner)
    scanner.scan = AsyncMock(side_effect=ScanError("repository_root_changed"))
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    service = RepositoryService(
        session,
        path_validator=MagicMock(),
        preview_store=AuthorizationPreviewStore(),
        scanner=scanner,
        scan_locks=ScanLockRegistry(),
    )
    store = MagicMock()
    store.snapshot = AsyncMock(return_value=snapshot)
    store.repository = AsyncMock(return_value=changed)
    service.store = store

    with pytest.raises(ApiError) as raised:
        await service.scan(repository_id, 3, _CONTEXT)

    assert raised.value.code == "stale_authorization_epoch"
    store.cancel_pending_jobs.assert_not_called()
    session.rollback.assert_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scan_epoch_recheck_discards_result_without_persistence() -> None:
    repository_id = uuid4()
    snapshot = _snapshot(repository_id, epoch=2)
    changed = SimpleNamespace(
        id=repository_id,
        authorization_status="authorized",
        authorization_epoch=3,
        root_identity=snapshot.root_identity,
        normalized_root_key=snapshot.normalized_root_key,
        policy_version=snapshot.policy_version,
    )
    source = SimpleNamespace(status="active")
    session = MagicMock(spec=AsyncSession)
    session.rollback = AsyncMock()
    service = _service(session, MagicMock(), AuthorizationPreviewStore())
    store = MagicMock()
    store.repository = AsyncMock(return_value=changed)
    store.source_for_repository = AsyncMock(return_value=source)
    service.store = store

    with pytest.raises(ApiError) as raised:
        await service._persist_scan(snapshot, _scan_result(eligible_files=1), _CONTEXT)

    assert raised.value.code == "stale_authorization_epoch"
    store.create_or_get_version.assert_not_called()
    session.rollback.assert_awaited_once()


def _scan_result(*, eligible_files: int) -> ScanResult:
    return ScanResult(
        entries=(),
        manifest_hash="a" * 64,
        stats=ScanStats(
            directories_visited=1,
            files_seen=eligible_files,
            eligible_files=eligible_files,
            eligible_bytes=eligible_files * 10,
            skipped={},
        ),
    )


def _service(
    session: AsyncSession, validator: MagicMock, previews: AuthorizationPreviewStore
) -> RepositoryService:
    return RepositoryService(
        session,
        path_validator=validator,
        preview_store=previews,
        scanner=MagicMock(spec=LocalRepositoryScanner),
        scan_locks=ScanLockRegistry(),
    )


def _candidate() -> RepositoryPathCandidate:
    return RepositoryPathCandidate(
        canonical_path=r"D:\workbench\safe-project",
        normalized_root_key=r"d:\workbench\safe-project",
        root_identity=RootIdentity("1:2"),
        display_name="safe-project",
    )


def _snapshot(repository_id: object, *, epoch: int) -> RepositorySnapshot:
    assert not isinstance(repository_id, str)
    return RepositorySnapshot(
        id=repository_id,  # type: ignore[arg-type]
        space_id=uuid4(),
        source_id=uuid4(),
        name="safe-project",
        canonical_root_path=r"D:\workbench\safe-project",
        normalized_root_key=r"d:\workbench\safe-project",
        root_identity="1:2",
        authorization_epoch=epoch,
        policy_version="local-repository-v1",
    )


def _summary(repository_id: object, space_id: object, *, epoch: int) -> RepositorySummaryData:
    del space_id
    return RepositorySummaryData(
        id=repository_id,  # type: ignore[arg-type]
        name="safe-project",
        canonical_root_path=r"D:\workbench\safe-project",
        authorization_status="authorized",
        authorization_epoch=epoch,
        authorized_at=datetime.now(UTC),
        revoked_at=None,
        scan_state="not_scanned",
        manifest_hash=None,
        eligible_files=0,
        eligible_bytes=0,
        indexing_state="not_queued",
    )
