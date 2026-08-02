from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.canonical_document import CanonicalBlock
from knowledge_workbench.application.source_ingestion import _validate_idempotency_key
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    Job,
    JobKind,
    JobStatus,
    OutboxEvent,
    ParseArtifactStatus,
    ParseRequestKind,
    ParseStatus,
    ProcessingStatus,
    Source,
    SourceParseArtifact,
    SourceParseRequest,
    SourceSection,
    SourceVersion,
)


@dataclass(frozen=True, slots=True)
class ScheduledParse:
    job: Job
    artifact: SourceParseArtifact


@dataclass(frozen=True, slots=True)
class SectionsPage:
    items: list[SourceSection]
    artifact: SourceParseArtifact
    next_cursor: str | None


class SourceParsingService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def schedule_initial_parse(
        self,
        session: AsyncSession,
        *,
        source: Source,
        version: SourceVersion,
    ) -> ScheduledParse:
        existing_request = await session.scalar(
            select(SourceParseRequest).where(
                SourceParseRequest.source_version_id == version.id,
                SourceParseRequest.idempotency_key == f"initial:{version.id}",
            )
        )
        if existing_request is not None:
            artifact = await session.get(
                SourceParseArtifact, existing_request.parse_artifact_id
            )
            if artifact is None:
                raise AppError(
                    "parse_request_inconsistent",
                    "The initial parse request result is unavailable.",
                    status_code=500,
                )
            job = await session.get(Job, existing_request.job_id)
            if job is None:
                raise AppError(
                    "parse_request_inconsistent",
                    "The initial parse request job is unavailable.",
                    status_code=500,
                )
            return ScheduledParse(job, artifact)
        existing = await self._active_parse(session, version.id)
        if existing is not None:
            request = await session.scalar(
                select(SourceParseRequest).where(
                    SourceParseRequest.parse_artifact_id == existing.id
                )
            )
            job = await session.get(Job, request.job_id) if request else None
            if job is None:
                raise AppError(
                    "parse_request_inconsistent",
                    "The active parse request job is unavailable.",
                    status_code=500,
                )
            return ScheduledParse(job, existing)
        revision = (
            await session.scalar(
                select(func.max(SourceParseArtifact.revision)).where(
                    SourceParseArtifact.source_version_id == version.id
                )
            )
            or 0
        ) + 1
        job = self._new_parse_job(source.space_id, version.id, revision=revision)
        session.add(job)
        artifact = self._new_artifact(version.id, revision=revision)
        session.add(artifact)
        request = SourceParseRequest(
            id=uuid4(),
            source_version_id=version.id,
            parse_artifact_id=artifact.id,
            job_id=job.id,
            kind=ParseRequestKind.INITIAL.value,
            idempotency_key=f"initial:{version.id}",
            request_hash=_parse_request_hash(version.id, "initial"),
        )
        session.add(request)
        self._queue(session, source.space_id, job, artifact, request.id)
        version.parse_status = ParseStatus.QUEUED.value
        await session.flush()
        return ScheduledParse(job, artifact)

    async def reparse(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        source_id: UUID,
        version_id: UUID,
        idempotency_key: str,
    ) -> ScheduledParse:
        key = _validate_idempotency_key(idempotency_key)
        source = await session.scalar(
            select(Source).where(
                Source.id == source_id,
                Source.space_id == space_id,
                Source.deleted_at.is_(None),
            )
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
                "source_version_not_found", "Source version was not found.", status_code=404
            )
        if (
            version.processing_status != ProcessingStatus.READY.value
            or version.storage_key is None
            or version.content_sha256 is None
        ):
            raise AppError(
                "source_version_not_ready",
                "The source version must finish ingestion before it can be reparsed.",
                status_code=409,
            )
        request_hash = _parse_request_hash(version.id, "reparse")
        existing_request = await session.scalar(
            select(SourceParseRequest).where(
                SourceParseRequest.source_version_id == version.id,
                SourceParseRequest.idempotency_key == key,
            )
        )
        if existing_request is not None:
            if existing_request.request_hash != request_hash:
                raise AppError(
                    "idempotency_conflict",
                    "This Idempotency-Key was used for another parse request.",
                    status_code=409,
                )
            artifact = await session.get(SourceParseArtifact, existing_request.parse_artifact_id)
            job = await session.get(Job, existing_request.job_id)
            if artifact is None or job is None:
                raise AppError(
                    "parse_request_inconsistent",
                    "The parse request result is unavailable.",
                    status_code=500,
                )
            return ScheduledParse(job, artifact)
        if await self._active_parse(session, version.id) is not None:
            raise AppError(
                "parse_already_active",
                "This source version already has an active parse.",
                status_code=409,
            )
        revision = (
            await session.scalar(
                select(func.max(SourceParseArtifact.revision)).where(
                    SourceParseArtifact.source_version_id == version.id
                )
            )
            or 0
        ) + 1
        artifact = self._new_artifact(version.id, revision=revision)
        request_id = uuid4()
        job = self._new_parse_job(space_id, version.id, revision=revision)
        session.add_all(
            [
                artifact,
                job,
                SourceParseRequest(
                    id=request_id,
                    source_version_id=version.id,
                    parse_artifact_id=artifact.id,
                    job_id=job.id,
                    kind=ParseRequestKind.REPARSE.value,
                    idempotency_key=key,
                    request_hash=request_hash,
                ),
            ]
        )
        version.parse_status = ParseStatus.QUEUED.value
        self._queue(session, space_id, job, artifact, request_id)
        await session.flush()
        return ScheduledParse(job, artifact)

    async def source_details(
        self, session: AsyncSession, *, space_id: UUID, source_id: UUID
    ) -> tuple[Source, list[tuple[SourceVersion, Job | None, SourceParseArtifact | None, int]]]:
        source = await session.scalar(
            select(Source).where(
                Source.id == source_id,
                Source.space_id == space_id,
                Source.deleted_at.is_(None),
            )
        )
        if source is None:
            raise AppError("source_not_found", "Source was not found.", status_code=404)
        versions = list(
            await session.scalars(
                select(SourceVersion)
                .where(SourceVersion.source_id == source.id)
                .order_by(SourceVersion.version_number.desc())
            )
        )
        results: list[tuple[SourceVersion, Job | None, SourceParseArtifact | None, int]] = []
        for version in versions:
            job = await session.scalar(
                select(Job)
                .join(SourceParseRequest, SourceParseRequest.job_id == Job.id)
                .where(SourceParseRequest.source_version_id == version.id)
                .order_by(SourceParseRequest.created_at.desc())
            )
            artifact = (
                await session.get(SourceParseArtifact, version.current_parse_artifact_id)
                if version.current_parse_artifact_id
                else None
            )
            count = 0
            if artifact is not None:
                count = (
                    await session.scalar(
                        select(func.count(SourceSection.id)).where(
                            SourceSection.parse_artifact_id == artifact.id
                        )
                    )
                    or 0
                )
            results.append((version, job, artifact, count))
        return source, results

    async def list_sections(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        source_id: UUID,
        version_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> SectionsPage:
        if limit < 1 or limit > 100:
            raise AppError(
                "invalid_page_limit", "Limit must be between 1 and 100.", status_code=422
            )
        version = await session.scalar(
            select(SourceVersion)
            .join(Source, Source.id == SourceVersion.source_id)
            .where(
                SourceVersion.id == version_id,
                SourceVersion.source_id == source_id,
                Source.space_id == space_id,
                Source.deleted_at.is_(None),
            )
        )
        if version is None:
            raise AppError(
                "source_version_not_found", "Source version was not found.", status_code=404
            )
        if version.current_parse_artifact_id is None:
            raise AppError(
                "source_not_parsed",
                "This source version has no ready parse artifact.",
                status_code=409,
            )
        artifact = await session.scalar(
            select(SourceParseArtifact).where(
                SourceParseArtifact.id == version.current_parse_artifact_id,
                SourceParseArtifact.source_version_id == version.id,
                SourceParseArtifact.status == ParseArtifactStatus.READY.value,
            )
        )
        if artifact is None:
            raise AppError(
                "source_not_parsed",
                "This source version has no ready parse artifact.",
                status_code=409,
            )
        query = select(SourceSection).where(SourceSection.parse_artifact_id == artifact.id)
        if cursor:
            cursor_artifact_id, ordinal, section_id = _decode_cursor(cursor)
            if cursor_artifact_id != artifact.id:
                raise AppError(
                    "stale_cursor",
                    "The sections cursor belongs to a different parse revision.",
                    status_code=409,
                )
            query = query.where(
                or_(
                    SourceSection.ordinal > ordinal,
                    and_(SourceSection.ordinal == ordinal, SourceSection.id > section_id),
                )
            )
        rows = list(
            await session.scalars(
                query.order_by(SourceSection.ordinal, SourceSection.id).limit(limit + 1)
            )
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = _encode_cursor(items[-1]) if has_more and items else None
        return SectionsPage(items, artifact, next_cursor)

    def _new_artifact(self, version_id: UUID, *, revision: int) -> SourceParseArtifact:
        return SourceParseArtifact(
            id=uuid4(),
            source_version_id=version_id,
            revision=revision,
            parser_name=self._settings.parser_name,
            parser_version=self._settings.parser_version,
            parser_config={
                "ocr": self._settings.parse_enable_ocr,
                "ocr_languages": self._settings.parse_ocr_languages,
                "max_pages": self._settings.parse_max_pages,
            },
            status=ParseArtifactStatus.QUEUED.value,
        )

    @staticmethod
    async def _active_parse(
        session: AsyncSession, version_id: UUID
    ) -> SourceParseArtifact | None:
        artifact: SourceParseArtifact | None = await session.scalar(
            select(SourceParseArtifact).where(
                SourceParseArtifact.source_version_id == version_id,
                SourceParseArtifact.status.in_(
                    [ParseArtifactStatus.QUEUED.value, ParseArtifactStatus.PARSING.value]
                ),
            )
        )
        return artifact

    @staticmethod
    def _new_parse_job(space_id: UUID, version_id: UUID, *, revision: int) -> Job:
        return Job(
            id=uuid4(),
            space_id=space_id,
            source_version_id=version_id,
            kind=JobKind.SOURCE_PARSE.value,
            status=JobStatus.QUEUED.value,
            progress=0,
            idempotency_key=f"source-parse:{version_id}:revision:{revision}",
            attempt_count=0,
            retryable=False,
        )

    @staticmethod
    def _queue(
        session: AsyncSession,
        space_id: UUID,
        job: Job,
        artifact: SourceParseArtifact,
        request_id: UUID,
    ) -> None:
        session.add(
            OutboxEvent(
                id=uuid4(),
                space_id=space_id,
                aggregate_type="job",
                aggregate_id=job.id,
                event_type="job.source_parse.requested",
                deduplication_key=f"source-parse:{artifact.id}:{request_id}",
                payload={"job_id": str(job.id), "space_id": str(space_id)},
            )
        )


def section_from_block(
    *,
    block: CanonicalBlock,
    section_id: UUID,
    space_id: UUID,
    source_id: UUID,
    version_id: UUID,
    artifact_id: UUID,
    revision: int,
    content_hash: str,
) -> SourceSection:
    bbox = None
    if block.provenance.bbox is not None:
        value = block.provenance.bbox
        bbox = {
            "left": value.left,
            "top": value.top,
            "right": value.right,
            "bottom": value.bottom,
            "origin": value.origin,
            "page_width": value.page_width,
            "page_height": value.page_height,
            "unit": value.unit,
        }
    locator = {
        "sourceId": str(source_id),
        "sourceVersionId": str(version_id),
        "parseArtifactId": str(artifact_id),
        "parseRevision": revision,
        "sectionId": str(section_id),
        "locatorType": "page_heading_paragraph",
        "page": block.provenance.page_number,
        "headingPath": list(block.heading_path),
        "paragraphIndex": block.paragraph_index,
        "bbox": bbox,
        "quoteHash": block.quote_hash,
    }
    return SourceSection(
        id=section_id,
        parse_artifact_id=artifact_id,
        source_version_id=version_id,
        space_id=space_id,
        block_id=block.block_id,
        parent_block_id=block.parent_block_id,
        ordinal=block.ordinal,
        block_type=block.block_type,
        title=block.title,
        text=block.text,
        heading_path=list(block.heading_path),
        page_number=block.provenance.page_number,
        paragraph_index=block.paragraph_index,
        bbox=bbox,
        locator=locator,
        quote_hash=block.quote_hash,
        content_hash=content_hash,
        provenance={
            "source_ref": block.provenance.source_ref,
            **block.provenance.metadata,
        },
    )


def _parse_request_hash(version_id: UUID, kind: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"kind": kind, "source_version_id": str(version_id)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _encode_cursor(section: SourceSection) -> str:
    payload = json.dumps(
        {
            "artifact_id": str(section.parse_artifact_id),
            "id": str(section.id),
            "ordinal": section.ordinal,
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[UUID, int, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        return UUID(payload["artifact_id"]), int(payload["ordinal"]), UUID(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AppError(
            "invalid_cursor", "The sections cursor is invalid.", status_code=422
        ) from exc
