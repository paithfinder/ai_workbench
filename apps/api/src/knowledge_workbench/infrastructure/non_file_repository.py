from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.non_file_ingestion import (
    NonFileIngestionClaim,
    NonFileIngestionRecord,
    NonFileIngestionRepository,
    PastedTextIngestionRequest,
    WebSnapshotIngestionRequest,
)
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    AcquisitionType,
    Job,
    JobKind,
    JobStatus,
    KnowledgeSpace,
    OutboxEvent,
    ParseStatus,
    ProcessingStatus,
    Source,
    SourceCreateRequest,
    SourceKind,
    SourceStatus,
    SourceVersion,
)

_CLAIM_TTL = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class CreatedNonFileSource:
    source: Source
    version: SourceVersion
    job: Job


class SqlAlchemyNonFileIngestionRepository(NonFileIngestionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        *,
        space_id: UUID,
        idempotency_key: str,
        request_hash: str,
    ) -> NonFileIngestionClaim:
        space = await self._session.scalar(
            select(KnowledgeSpace)
            .where(KnowledgeSpace.id == space_id)
            .with_for_update()
        )
        if space is None:
            raise AppError(
                "knowledge_space_not_found", "Knowledge space was not found.", status_code=404
            )
        now = datetime.now(UTC)
        existing = await self._session.scalar(
            select(SourceCreateRequest)
            .where(
                SourceCreateRequest.space_id == space_id,
                SourceCreateRequest.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise AppError(
                    "idempotency_conflict",
                    "This Idempotency-Key was already used with different source content.",
                    status_code=409,
                )
            if existing.source_id is not None:
                record = await self._existing_record(
                    space_id, existing.source_id, request_hash
                )
                return NonFileIngestionClaim(
                    request_hash=request_hash,
                    lease_token=None,
                    existing_record=record,
                )
            if (
                existing.lease_token is not None
                and existing.lease_expires_at is not None
                and existing.lease_expires_at > now
            ):
                raise AppError(
                    "source_creation_in_progress",
                    "This source request is already being processed.",
                    status_code=409,
                )
            existing.lease_token = uuid4()
            existing.lease_expires_at = now + _CLAIM_TTL
            await self._session.flush()
            return NonFileIngestionClaim(
                request_hash=request_hash,
                lease_token=existing.lease_token,
            )

        lease_token = uuid4()
        self._session.add(
            SourceCreateRequest(
                id=uuid4(),
                space_id=space_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                source_id=None,
                lease_token=lease_token,
                lease_expires_at=now + _CLAIM_TTL,
            )
        )
        await self._session.flush()
        return NonFileIngestionClaim(
            request_hash=request_hash,
            lease_token=lease_token,
        )

    async def finalize_pasted_text(
        self, request: PastedTextIngestionRequest
    ) -> NonFileIngestionRecord:
        return await self._finalize(
            space_id=request.space_id,
            title=request.title,
            idempotency_key=request.idempotency_key,
            request_hash=request.request_hash,
            lease_token=request.lease_token,
            kind=SourceKind.PASTED_TEXT,
            acquisition_type=AcquisitionType.PASTED_TEXT,
            source_uri=None,
            acquisition_metadata={},
            media_type="text/plain",
            size_bytes=request.size_bytes,
            content_sha256=request.content_sha256,
            storage_key=request.storage_key,
            object_etag=request.object_etag,
        )

    async def finalize_web_snapshot(
        self, request: WebSnapshotIngestionRequest
    ) -> NonFileIngestionRecord:
        return await self._finalize(
            space_id=request.space_id,
            title=request.title,
            idempotency_key=request.idempotency_key,
            request_hash=request.request_hash,
            lease_token=request.lease_token,
            kind=SourceKind.WEB,
            acquisition_type=AcquisitionType.WEB_FETCH,
            source_uri=request.final_url,
            acquisition_metadata={
                "requested_url": request.requested_url,
                "final_url": request.final_url,
                "fetched_at": request.fetched_at.isoformat(),
                "headers": request.headers,
                "media_type": request.media_type,
                "size_bytes": request.size_bytes,
                "content_sha256": request.content_sha256,
            },
            media_type=request.media_type,
            size_bytes=request.size_bytes,
            content_sha256=request.content_sha256,
            storage_key=request.storage_key,
            object_etag=request.object_etag,
        )

    async def release_claim(
        self,
        *,
        space_id: UUID,
        idempotency_key: str,
        lease_token: UUID,
    ) -> None:
        reservation = await self._session.scalar(
            select(SourceCreateRequest)
            .where(
                SourceCreateRequest.space_id == space_id,
                SourceCreateRequest.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if (
            reservation is not None
            and reservation.source_id is None
            and reservation.lease_token == lease_token
        ):
            reservation.lease_token = None
            reservation.lease_expires_at = None
            await self._session.flush()

    async def result(
        self, *, space_id: UUID, source_id: UUID, version_id: UUID, job_id: UUID
    ) -> CreatedNonFileSource:
        source = await self._session.scalar(
            select(Source).where(Source.id == source_id, Source.space_id == space_id)
        )
        version = await self._session.scalar(
            select(SourceVersion).where(
                SourceVersion.id == version_id, SourceVersion.source_id == source_id
            )
        )
        job = await self._session.scalar(
            select(Job).where(Job.id == job_id, Job.space_id == space_id)
        )
        if source is None or version is None or job is None:
            raise AppError(
                "non_file_ingestion_inconsistent",
                "The non-file source result is incomplete.",
                status_code=500,
            )
        return CreatedNonFileSource(source=source, version=version, job=job)

    async def _finalize(
        self,
        *,
        space_id: UUID,
        title: str,
        idempotency_key: str,
        request_hash: str,
        lease_token: UUID,
        kind: SourceKind,
        acquisition_type: AcquisitionType,
        source_uri: str | None,
        acquisition_metadata: dict[str, Any],
        media_type: str,
        size_bytes: int,
        content_sha256: str,
        storage_key: str,
        object_etag: str,
    ) -> NonFileIngestionRecord:
        reservation = await self._session.scalar(
            select(SourceCreateRequest)
            .where(
                SourceCreateRequest.space_id == space_id,
                SourceCreateRequest.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if reservation is None or reservation.request_hash != request_hash:
            raise AppError(
                "non_file_ingestion_claim_lost",
                "The non-file ingestion reservation is unavailable.",
                status_code=409,
            )
        if reservation.source_id is not None:
            return await self._existing_record(
                space_id, reservation.source_id, request_hash
            )
        if reservation.lease_token != lease_token:
            raise AppError(
                "non_file_ingestion_claim_lost",
                "The non-file ingestion reservation was superseded.",
                status_code=409,
            )

        source = Source(
            id=uuid4(),
            space_id=space_id,
            kind=kind.value,
            title=title,
            status=SourceStatus.PENDING.value,
        )
        now = datetime.now(UTC)
        version = SourceVersion(
            id=uuid4(),
            source_id=source.id,
            version_number=1,
            content_sha256=content_sha256,
            expected_content_sha256=None,
            original_filename=None,
            media_type=media_type,
            size_bytes=size_bytes,
            upload_storage_key=None,
            storage_key=storage_key,
            object_etag=object_etag,
            upload_idempotency_key=None,
            upload_request_hash=None,
            completion_idempotency_key=idempotency_key,
            upload_expires_at=None,
            completed_at=now,
            acquisition_type=acquisition_type.value,
            source_uri=source_uri,
            acquisition_metadata=acquisition_metadata,
            processing_status=ProcessingStatus.PENDING.value,
            parse_status=ParseStatus.NOT_STARTED.value,
        )
        job = Job(
            id=uuid4(),
            space_id=space_id,
            source_version_id=version.id,
            kind=JobKind.SOURCE_INGEST.value,
            status=JobStatus.QUEUED.value,
            progress=0,
            idempotency_key=f"source-ingest:{version.id}",
            attempt_count=0,
            retryable=False,
        )
        reservation.source_id = source.id
        reservation.lease_token = None
        reservation.lease_expires_at = None
        self._session.add_all(
            [
                source,
                version,
                job,
                OutboxEvent(
                    id=uuid4(),
                    space_id=space_id,
                    aggregate_type="job",
                    aggregate_id=job.id,
                    event_type="job.source_ingest.requested",
                    deduplication_key=f"source-ingest:{job.id}:attempt:1",
                    payload={"job_id": str(job.id), "space_id": str(space_id)},
                ),
            ]
        )
        await self._session.flush()
        return NonFileIngestionRecord(
            source_id=source.id,
            version_id=version.id,
            job_id=job.id,
            storage_key=storage_key,
            request_hash=request_hash,
        )

    async def _existing_record(
        self, space_id: UUID, source_id: UUID, request_hash: str
    ) -> NonFileIngestionRecord:
        version = await self._session.scalar(
            select(SourceVersion)
            .where(SourceVersion.source_id == source_id)
            .order_by(SourceVersion.version_number.desc())
        )
        if version is None or version.storage_key is None:
            raise AppError(
                "non_file_ingestion_inconsistent",
                "The prior non-file ingestion result is missing.",
                status_code=500,
            )
        job = await self._session.scalar(
            select(Job).where(
                Job.space_id == space_id,
                Job.source_version_id == version.id,
                Job.kind == JobKind.SOURCE_INGEST.value,
            )
        )
        if job is None:
            raise AppError(
                "non_file_ingestion_inconsistent",
                "The prior non-file ingestion job is missing.",
                status_code=500,
            )
        return NonFileIngestionRecord(
            source_id=source_id,
            version_id=version.id,
            job_id=job.id,
            storage_key=version.storage_key,
            request_hash=request_hash,
        )
