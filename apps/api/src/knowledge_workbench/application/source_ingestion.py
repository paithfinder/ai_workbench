from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.file_validation import (
    inspect_content,
    validate_content_signature,
    validate_upload_declaration,
)
from knowledge_workbench.application.ports.object_storage import (
    ObjectStorage,
    PresignedUpload,
    StoredObject,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
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


@dataclass(frozen=True, slots=True)
class UploadDeclaration:
    original_filename: str
    media_type: str
    size_bytes: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class UploadReservation:
    version: SourceVersion
    upload: PresignedUpload


@dataclass(frozen=True, slots=True)
class CompletedUpload:
    source: Source
    version: SourceVersion
    job: Job


class SourceIngestionService:
    def __init__(self, storage: ObjectStorage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    async def create_source(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        kind: SourceKind,
        title: str,
        idempotency_key: str,
    ) -> Source:
        space = await session.scalar(
            select(KnowledgeSpace)
            .where(KnowledgeSpace.id == space_id)
            .with_for_update()
        )
        if space is None:
            raise AppError(
                "knowledge_space_not_found",
                "Knowledge space was not found.",
                status_code=404,
            )
        key = _validate_idempotency_key(idempotency_key)
        if kind not in {SourceKind.PDF, SourceKind.MARKDOWN, SourceKind.TEXT}:
            raise AppError(
                "unsupported_source_kind",
                "D2 supports PDF, Markdown, and TXT file sources only.",
                status_code=422,
            )
        normalized_title = title.strip()
        if not normalized_title:
            raise AppError("invalid_source_title", "Source title cannot be empty.", status_code=422)
        request_hash = hashlib.sha256(
            json.dumps(
                {"kind": kind.value, "title": normalized_title},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        existing_request = await session.scalar(
            select(SourceCreateRequest).where(
                SourceCreateRequest.space_id == space_id,
                SourceCreateRequest.idempotency_key == key,
            )
        )
        if existing_request is not None:
            if existing_request.request_hash != request_hash:
                raise AppError(
                    "idempotency_conflict",
                    "This Idempotency-Key was already used with a different source request.",
                    status_code=409,
                )
            existing_source = await session.get(Source, existing_request.source_id)
            if existing_source is None:
                raise AppError(
                    "source_create_inconsistent",
                    "The source creation result is no longer available.",
                    status_code=500,
                )
            return existing_source
        source = Source(
            id=uuid4(),
            space_id=space_id,
            kind=kind.value,
            title=normalized_title,
            status=SourceStatus.PENDING.value,
        )
        session.add(source)
        session.add(
            SourceCreateRequest(
                id=uuid4(),
                space_id=source.space_id,
                idempotency_key=key,
                request_hash=request_hash,
                source_id=source.id,
            )
        )
        await session.flush()
        return source

    async def list_sources(self, session: AsyncSession, *, space_id: UUID) -> list[Source]:
        await self._require_space(session, space_id)
        result = await session.scalars(
            select(Source)
            .where(Source.space_id == space_id, Source.deleted_at.is_(None))
            .order_by(Source.created_at.desc(), Source.id.desc())
        )
        return list(result)

    async def get_source(
        self, session: AsyncSession, *, space_id: UUID, source_id: UUID
    ) -> Source:
        source = await session.scalar(self._source_query(space_id, source_id))
        if source is None:
            raise AppError("source_not_found", "Source was not found.", status_code=404)
        return source

    async def list_versions(
        self, session: AsyncSession, *, space_id: UUID, source_id: UUID
    ) -> list[SourceVersion]:
        await self.get_source(session, space_id=space_id, source_id=source_id)
        result = await session.scalars(
            select(SourceVersion)
            .where(SourceVersion.source_id == source_id)
            .order_by(SourceVersion.version_number.desc())
        )
        return list(result)

    async def reserve_upload(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        source_id: UUID,
        declaration: UploadDeclaration,
        idempotency_key: str,
    ) -> UploadReservation:
        key = _validate_idempotency_key(idempotency_key)
        source = await session.scalar(
            self._source_query(space_id, source_id).with_for_update()
        )
        if source is None:
            raise AppError("source_not_found", "Source was not found.", status_code=404)
        kind = SourceKind(source.kind)
        filename, media_type = validate_upload_declaration(
            kind=kind,
            original_filename=declaration.original_filename,
            media_type=declaration.media_type,
            size_bytes=declaration.size_bytes,
            content_sha256=declaration.content_sha256,
            max_size_bytes=self._settings.max_upload_size_bytes,
        )
        request_hash = _request_hash(
            filename=filename,
            media_type=media_type,
            size_bytes=declaration.size_bytes,
            content_sha256=declaration.content_sha256,
        )
        existing = await session.scalar(
            select(SourceVersion).where(
                SourceVersion.source_id == source_id,
                SourceVersion.upload_idempotency_key == key,
            )
        )
        if existing is not None:
            if existing.upload_request_hash != request_hash:
                raise AppError(
                    "idempotency_conflict",
                    "This Idempotency-Key was already used with a different upload request.",
                    status_code=409,
                )
            if existing.completed_at is not None:
                raise AppError(
                    "upload_already_completed",
                    "This upload reservation is already completed and cannot be reopened.",
                    status_code=409,
                )
            now = datetime.now(UTC)
            remaining = existing.upload_expires_at - now
            if remaining <= timedelta(0):
                raise AppError(
                    "upload_reservation_expired",
                    "The upload reservation expired. Use a new Idempotency-Key.",
                    status_code=409,
                )
            upload = await self._storage.presign_post(
                existing.upload_storage_key,
                existing.media_type,
                existing.size_bytes,
                remaining,
            )
            return UploadReservation(existing, upload)

        max_version = await session.scalar(
            select(func.max(SourceVersion.version_number)).where(
                SourceVersion.source_id == source_id
            )
        )
        version_id = uuid4()
        upload_key = f"uploads/{space_id}/{source_id}/{version_id}/{filename}"
        expires_at = datetime.now(UTC) + timedelta(
            seconds=self._settings.s3_presign_ttl_seconds
        )
        version = SourceVersion(
            id=version_id,
            source_id=source_id,
            version_number=(max_version or 0) + 1,
            content_sha256=None,
            expected_content_sha256=declaration.content_sha256,
            original_filename=filename,
            media_type=media_type,
            size_bytes=declaration.size_bytes,
            upload_storage_key=upload_key,
            storage_key=None,
            object_etag=None,
            upload_idempotency_key=key,
            upload_request_hash=request_hash,
            completion_idempotency_key=None,
            upload_expires_at=expires_at,
            completed_at=None,
            processing_status=ProcessingStatus.PENDING.value,
            parse_status=ParseStatus.NOT_STARTED.value,
        )
        session.add(version)
        await session.flush()
        upload = await self._storage.presign_post(
            upload_key,
            media_type,
            declaration.size_bytes,
            timedelta(seconds=self._settings.s3_presign_ttl_seconds),
        )
        return UploadReservation(version, upload)

    async def complete_upload(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        source_id: UUID,
        version_id: UUID,
        idempotency_key: str,
    ) -> CompletedUpload:
        key = _validate_idempotency_key(idempotency_key)
        source = await session.scalar(
            self._source_query(space_id, source_id).with_for_update()
        )
        if source is None:
            raise AppError("source_not_found", "Source was not found.", status_code=404)
        version = await session.scalar(
            select(SourceVersion)
            .where(SourceVersion.id == version_id, SourceVersion.source_id == source_id)
            .with_for_update()
        )
        if version is None:
            raise AppError(
                "source_version_not_found",
                "Source version was not found.",
                status_code=404,
            )

        if version.completed_at is not None:
            if version.completion_idempotency_key != key:
                raise AppError(
                    "idempotency_conflict",
                    "This source version was already completed with another Idempotency-Key.",
                    status_code=409,
                )
            job = await self._get_ingest_job(session, space_id, version.id)
            return CompletedUpload(source, version, job)

        suffix = PurePath(version.original_filename).suffix.lower()
        immutable_key = (
            f"sources/{space_id}/{source_id}/versions/{version.id}/"
            f"{version.expected_content_sha256}{suffix}"
        )
        recovered = await self._validated_immutable_object(version, immutable_key)
        if recovered is not None:
            return await self._finalize_completion(
                session,
                source=source,
                version=version,
                promoted=recovered,
                immutable_key=immutable_key,
                idempotency_key=key,
            )
        try:
            existing_immutable = await self._storage.stat(immutable_key)
        except AppError as exc:
            if exc.code != "uploaded_object_missing":
                raise
        else:
            del existing_immutable
            await self._storage.delete(immutable_key)
            raise AppError(
                "immutable_object_conflict",
                "The immutable object key contains content that does not match this version.",
                status_code=409,
            )

        if version.upload_expires_at <= datetime.now(UTC):
            raise AppError(
                "upload_reservation_expired",
                "The upload reservation expired. Reserve a new upload and try again.",
                status_code=409,
            )

        stored = await self._storage.stat(version.upload_storage_key)
        if stored.size != version.size_bytes:
            raise AppError(
                "upload_size_mismatch",
                "The uploaded object size does not match the reserved file.",
                status_code=422,
            )
        if stored.media_type != version.media_type:
            raise AppError(
                "upload_media_type_mismatch",
                "The uploaded object media type does not match the reserved file.",
                status_code=422,
            )
        kind = SourceKind(source.kind)
        validation = await inspect_content(
            self._storage.iter_bytes(version.upload_storage_key),
            expected_size_bytes=version.size_bytes,
            max_size_bytes=self._settings.max_upload_size_bytes,
            validate_text=kind in {SourceKind.MARKDOWN, SourceKind.TEXT},
        )
        if validation.sha256 != version.expected_content_sha256:
            raise AppError(
                "upload_checksum_mismatch",
                "The uploaded object checksum does not match the reserved file.",
                status_code=422,
            )
        validate_content_signature(kind, validation)

        reread = await self._storage.stat(version.upload_storage_key)
        if reread.etag != stored.etag or reread.size != stored.size:
            raise AppError(
                "uploaded_object_changed",
                "The uploaded object changed during validation. Upload it again.",
                status_code=409,
            )

        duplicate = await session.scalar(
            select(SourceVersion.id).where(
                SourceVersion.source_id == source_id,
                SourceVersion.content_sha256 == validation.sha256,
                SourceVersion.id != version.id,
            )
        )
        if duplicate is not None:
            raise AppError(
                "source_content_already_exists",
                "This source already has a version with identical content.",
                status_code=409,
            )

        suffix = PurePath(version.original_filename).suffix.lower()
        immutable_key = (
            f"sources/{space_id}/{source_id}/versions/{version.id}/"
            f"{validation.sha256}{suffix}"
        )
        promoted = await self._storage.promote(
            version.upload_storage_key,
            immutable_key,
            stored.etag,
            validation.sha256,
        )
        verified = await self._validated_immutable_object(version, immutable_key)
        if verified is None:
            await self._storage.delete(immutable_key)
            raise AppError(
                "object_promotion_mismatch",
                "The immutable stored object does not match the validated upload.",
                status_code=503,
            )
        if verified.etag != promoted.etag:
            await self._storage.delete(immutable_key)
            raise AppError(
                "object_promotion_mismatch",
                "The immutable stored object changed after promotion.",
                status_code=503,
            )
        return await self._finalize_completion(
            session,
            source=source,
            version=version,
            promoted=verified,
            immutable_key=immutable_key,
            idempotency_key=key,
        )

    async def _validated_immutable_object(
        self,
        version: SourceVersion,
        immutable_key: str,
    ) -> StoredObject | None:
        try:
            stored = await self._storage.stat(immutable_key)
        except AppError as exc:
            if exc.code == "uploaded_object_missing":
                return None
            raise
        if stored.size != version.size_bytes or stored.media_type != version.media_type:
            return None
        validation = await inspect_content(
            self._storage.iter_bytes(immutable_key),
            expected_size_bytes=version.size_bytes,
            max_size_bytes=self._settings.max_upload_size_bytes,
            validate_text=False,
        )
        if validation.sha256 != version.expected_content_sha256:
            return None
        reread = await self._storage.stat(immutable_key)
        if reread.etag != stored.etag or reread.size != stored.size:
            return None
        return reread

    async def _finalize_completion(
        self,
        session: AsyncSession,
        *,
        source: Source,
        version: SourceVersion,
        promoted: StoredObject,
        immutable_key: str,
        idempotency_key: str,
    ) -> CompletedUpload:
        version.content_sha256 = version.expected_content_sha256
        version.storage_key = immutable_key
        version.object_etag = promoted.etag
        version.completion_idempotency_key = idempotency_key
        version.completed_at = datetime.now(UTC)
        job = Job(
            id=uuid4(),
            space_id=source.space_id,
            source_version_id=version.id,
            kind=JobKind.SOURCE_INGEST.value,
            status=JobStatus.QUEUED.value,
            progress=0,
            idempotency_key=f"source-ingest:{version.id}",
            attempt_count=0,
            retryable=False,
        )
        session.add(job)
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=source.space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type="job.source_ingest.requested",
                deduplication_key=f"source-ingest:{job.id}:attempt:1",
                payload={"job_id": str(job.id), "space_id": str(source.space_id)},
            )
        )
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=source.space_id,
                aggregate_type="source_version",
                aggregate_id=version.id,
                event_type="storage.staging_cleanup.requested",
                deduplication_key=f"staging-cleanup:{version.id}",
                payload={
                    "storage_key": version.upload_storage_key,
                    "space_id": str(source.space_id),
                },
            )
        )
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AppError(
                "source_content_already_exists",
                "This source already has a version with identical content.",
                status_code=409,
            ) from exc
        return CompletedUpload(source, version, job)

    @staticmethod
    def _source_query(space_id: UUID, source_id: UUID) -> Select[tuple[Source]]:
        return select(Source).where(
            Source.id == source_id,
            Source.space_id == space_id,
            Source.deleted_at.is_(None),
        )

    @staticmethod
    async def _require_space(session: AsyncSession, space_id: UUID) -> KnowledgeSpace:
        space = await session.get(KnowledgeSpace, space_id)
        if space is None:
            raise AppError(
                "knowledge_space_not_found", "Knowledge space was not found.", status_code=404
            )
        return space

    @staticmethod
    async def _get_ingest_job(
        session: AsyncSession, space_id: UUID, version_id: UUID
    ) -> Job:
        job = await session.scalar(
            select(Job).where(
                Job.space_id == space_id,
                Job.source_version_id == version_id,
                Job.kind == JobKind.SOURCE_INGEST.value,
            )
        )
        if job is None:
            raise AppError(
                "source_completion_inconsistent",
                "The completed source version has no ingestion job.",
                status_code=500,
            )
        return job


def _validate_idempotency_key(value: str) -> str:
    key = value.strip()
    if not key or len(key) > 255:
        raise AppError(
            "invalid_idempotency_key",
            "Idempotency-Key must contain between 1 and 255 characters.",
            status_code=422,
        )
    return key


def _request_hash(
    *, filename: str, media_type: str, size_bytes: int, content_sha256: str
) -> str:
    payload = json.dumps(
        {
            "content_sha256": content_sha256,
            "filename": filename,
            "media_type": media_type,
            "size_bytes": size_bytes,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
