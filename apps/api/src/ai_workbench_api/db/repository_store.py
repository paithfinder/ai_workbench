"""SQLAlchemy persistence operations for local repository authorization and scans."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ai_workbench_api.db.models import (
    AuditEvent,
    IndexJob,
    Repository,
    ScanManifestEntry,
    Source,
    SourceVersion,
    Space,
)
from ai_workbench_api.domain.repositories import (
    PERSONAL_SPACE_NAME,
    PERSONAL_SPACE_SLUG,
    AuditContext,
    RepositorySnapshot,
    RepositorySummaryData,
)
from ai_workbench_api.security.repository_paths import RepositoryPathCandidate
from ai_workbench_api.sources.local_repository_scanner import ScanResult


class RepositoryStore:
    """Execute repository SQL without filesystem access or implicit commits."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_personal_space(self) -> Space:
        await self.session.execute(
            insert(Space)
            .values(
                id=uuid.uuid4(),
                name=PERSONAL_SPACE_NAME,
                slug=PERSONAL_SPACE_SLUG,
                description="Singleton local development workspace",
            )
            .on_conflict_do_nothing(index_elements=[Space.slug])
        )
        result = await self.session.execute(select(Space).where(Space.slug == PERSONAL_SPACE_SLUG))
        return result.scalar_one()

    async def personal_space(self) -> Space | None:
        result = await self.session.execute(select(Space).where(Space.slug == PERSONAL_SPACE_SLUG))
        return result.scalar_one_or_none()

    async def repository_by_root_key(
        self, space_id: uuid.UUID, normalized_root_key: str, *, for_update: bool = False
    ) -> Repository | None:
        statement = select(Repository).where(
            Repository.space_id == space_id,
            Repository.provider == "local",
            Repository.normalized_root_key == normalized_root_key,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def repository(
        self, repository_id: uuid.UUID, *, for_update: bool = False
    ) -> Repository | None:
        statement = select(Repository).where(Repository.id == repository_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def source_for_repository(
        self, repository_id: uuid.UUID, *, for_update: bool = False
    ) -> Source | None:
        statement = select(Source).where(Source.repository_id == repository_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def create_authorized_repository(
        self, space: Space, candidate: RepositoryPathCandidate, now: datetime
    ) -> tuple[Repository, Source]:
        repository = Repository(
            space_id=space.id,
            provider="local",
            name=candidate.display_name,
            canonical_root_path=candidate.canonical_path,
            normalized_root_key=candidate.normalized_root_key,
            root_identity=candidate.root_identity.value,
            authorization_status="authorized",
            authorization_epoch=1,
            policy_version=candidate.policy_version,
            authorized_at=now,
        )
        self.session.add(repository)
        await self.session.flush()
        source = await self.create_source(space.id, repository)
        return repository, source

    async def create_source(self, space_id: uuid.UUID, repository: Repository) -> Source:
        source = Source(
            space_id=space_id,
            repository_id=repository.id,
            kind="repository",
            uri=f"local-repository://{repository.id}",
            title=repository.name,
            status="active",
            source_metadata={},
        )
        self.session.add(source)
        await self.session.flush()
        return source

    async def snapshot(self, repository_id: uuid.UUID) -> RepositorySnapshot | None:
        statement = (
            select(Repository, Source)
            .join(Source, Source.repository_id == Repository.id)
            .where(Repository.id == repository_id)
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        repository, source = row
        if (
            repository.authorization_status != "authorized"
            or source.status != "active"
            or repository.canonical_root_path is None
            or repository.normalized_root_key is None
            or repository.root_identity is None
            or repository.policy_version is None
        ):
            return None
        return RepositorySnapshot(
            id=repository.id,
            space_id=repository.space_id,
            source_id=source.id,
            name=repository.name,
            canonical_root_path=repository.canonical_root_path,
            normalized_root_key=repository.normalized_root_key,
            root_identity=repository.root_identity,
            authorization_epoch=repository.authorization_epoch,
            policy_version=repository.policy_version,
        )

    async def create_or_get_version(
        self, snapshot: RepositorySnapshot, result: ScanResult
    ) -> tuple[SourceVersion, bool]:
        existing = (
            await self.session.execute(
                select(SourceVersion).where(
                    SourceVersion.source_id == snapshot.source_id,
                    SourceVersion.content_hash == result.manifest_hash,
                    SourceVersion.authorization_epoch == snapshot.authorization_epoch,
                    SourceVersion.policy_version == snapshot.policy_version,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        version = SourceVersion(
            source_id=snapshot.source_id,
            version_identifier=f"scan:{snapshot.authorization_epoch}:{result.manifest_hash}",
            content_hash=result.manifest_hash,
            authorization_epoch=snapshot.authorization_epoch,
            policy_version=snapshot.policy_version,
            version_metadata={
                "policy_version": snapshot.policy_version,
                "directories_visited": result.stats.directories_visited,
                "files_seen": result.stats.files_seen,
                "eligible_files": result.stats.eligible_files,
                "eligible_bytes": result.stats.eligible_bytes,
                "skipped": dict(result.stats.skipped),
            },
        )
        self.session.add(version)
        await self.session.flush()
        self.session.add_all(
            ScanManifestEntry(
                source_version_id=version.id,
                relative_path=entry.relative_path,
                content_hash=entry.content_hash,
                size_bytes=entry.size_bytes,
                modified_ns=entry.modified_ns,
                file_identity=entry.file_identity,
            )
            for entry in result.entries
        )
        await self.session.flush()
        return version, True

    async def ensure_pending_job(
        self, space_id: uuid.UUID, version: SourceVersion
    ) -> IndexJob | None:
        if int(version.version_metadata.get("eligible_files", 0)) == 0:
            return None
        active = (
            await self.session.execute(
                select(IndexJob)
                .where(
                    IndexJob.source_version_id == version.id,
                    IndexJob.status.in_(("pending", "running")),
                )
                .order_by(IndexJob.attempt.desc(), IndexJob.id.desc())
                .with_for_update()
            )
        ).scalars().first()
        if active is not None:
            return active
        # Lock every existing attempt before deriving the next number. The repository row is
        # already locked by the service, serializing scans even across multiple API processes.
        highest_attempt = (
            await self.session.execute(
                select(func.coalesce(func.max(IndexJob.attempt), 0)).where(
                    IndexJob.source_version_id == version.id
                )
            )
        ).scalar_one()
        job = IndexJob(
            space_id=space_id,
            source_version_id=version.id,
            status="pending",
            attempt=highest_attempt + 1,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def cancel_pending_jobs(self, repository_id: uuid.UUID, now: datetime) -> None:
        versions = (
            select(SourceVersion.id).join(Source).where(Source.repository_id == repository_id)
        )
        await self.session.execute(
            update(IndexJob)
            .where(IndexJob.source_version_id.in_(versions), IndexJob.status == "pending")
            .values(status="cancelled", completed_at=now, error_code="authorization_revoked")
        )

    def add_audit(
        self,
        *,
        space_id: uuid.UUID,
        repository_id: uuid.UUID,
        action: str,
        context: AuditContext,
        payload: dict[str, object],
    ) -> None:
        self.session.add(
            AuditEvent(
                space_id=space_id,
                action=action,
                resource_type="repository",
                resource_id=repository_id,
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                payload=payload,
            )
        )

    async def summaries(self, space_id: uuid.UUID) -> list[RepositorySummaryData]:
        latest = aliased(SourceVersion)
        latest_id: Select[tuple[uuid.UUID]] = (
            select(SourceVersion.id)
            .where(
                SourceVersion.source_id == Source.id,
                SourceVersion.authorization_epoch == Repository.authorization_epoch,
                SourceVersion.policy_version == Repository.policy_version,
            )
            .order_by(SourceVersion.created_at.desc())
            .limit(1)
            .correlate(Source, Repository)
        )
        statement = (
            select(Repository, Source, latest, IndexJob)
            .outerjoin(Source, Source.repository_id == Repository.id)
            .outerjoin(latest, latest.id == latest_id.scalar_subquery())
            .outerjoin(IndexJob, IndexJob.id == (
                select(IndexJob.id)
                .where(IndexJob.source_version_id == latest.id)
                .order_by(IndexJob.attempt.desc(), IndexJob.created_at.desc(), IndexJob.id.desc())
                .limit(1)
                .correlate(latest)
                .scalar_subquery()
            ))
            .where(Repository.space_id == space_id, Repository.provider == "local")
            .order_by(Repository.name, Repository.id)
        )
        rows = (await self.session.execute(statement)).all()
        summaries: list[RepositorySummaryData] = []
        seen: set[uuid.UUID] = set()
        for repository, _source, version, job in rows:
            if repository.id in seen:
                continue
            seen.add(repository.id)
            metadata = version.version_metadata if version is not None else {}
            summaries.append(
                RepositorySummaryData(
                    id=repository.id,
                    name=repository.name,
                    canonical_root_path=repository.canonical_root_path or "",
                    authorization_status=repository.authorization_status,
                    authorization_epoch=repository.authorization_epoch,
                    authorized_at=repository.authorized_at,
                    revoked_at=repository.revoked_at,
                    scan_state=(
                        "manifest_ready"
                        if version is not None and repository.authorization_status == "authorized"
                        else "not_scanned"
                    ),
                    manifest_hash=(
                        version.content_hash
                        if version is not None and repository.authorization_status == "authorized"
                        else None
                    ),
                    eligible_files=int(metadata.get("eligible_files", 0)),
                    eligible_bytes=int(metadata.get("eligible_bytes", 0)),
                    indexing_state=job.status if job is not None else "not_queued",
                )
            )
        return summaries


def utcnow() -> datetime:
    return datetime.now(UTC)
