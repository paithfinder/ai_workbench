from __future__ import annotations

import asyncio
import hashlib
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from minio import Minio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_workbench.application.ports.document_parser import (
    DocumentParseError,
    DocumentParser,
    ParseInput,
    ParseResult,
)
from knowledge_workbench.application.ports.object_storage import ObjectStorage, StoredObject
from knowledge_workbench.application.source_parsing import section_from_block
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import (
    Job,
    JobAttempt,
    JobAttemptStatus,
    JobKind,
    JobStatus,
    ParseArtifactStatus,
    ParseStatus,
    Source,
    SourceParseArtifact,
    SourceSection,
    SourceVersion,
)
from knowledge_workbench.db.session import create_engine, create_session_factory
from knowledge_workbench.infrastructure.parsing.docling import DoclingDocumentParser
from knowledge_workbench.infrastructure.parsing.process import parse_docling_in_subprocess
from knowledge_workbench.infrastructure.storage.minio import MinioObjectStorage
from knowledge_workbench.worker.job_runner import (
    ClaimToken,
    JobRunner,
    PermanentJobError,
)
from knowledge_workbench.worker.job_runner import (
    mark_transient_failure as mark_job_transient_failure,
)
from knowledge_workbench.worker.job_runner import (
    mark_transient_failure_sync as mark_job_transient_failure_sync,
)


class SourceParseWorker(JobRunner):
    def __init__(
        self,
        settings: Settings,
        parser: DocumentParser,
        storage: ObjectStorage,
        heartbeat_session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        super().__init__(settings, JobKind.SOURCE_PARSE)
        self._parser = parser
        self._storage = storage
        self._heartbeat_sessions = heartbeat_session_factory
        self._parsed: dict[UUID, tuple[UUID, object]] = {}

    async def _run_claimed(
        self, session: AsyncSession, *, job_id: UUID, token: ClaimToken
    ) -> None:
        prepared = await self._prepare(session, job_id=job_id, token=token)
        if prepared is None:
            return
        version, source, artifact = prepared
        suffix = _source_suffix(version)
        temp_path: Path | None = None
        keys: dict[str, StoredObject] | None = None
        committed = False
        stop = asyncio.Event()
        heartbeat = None
        if self._heartbeat_sessions is not None:
            heartbeat = asyncio.create_task(self._heartbeat_loop(job_id, token, stop))
        try:
            temp_path = await self._download(version, suffix)
            parse_input = ParseInput(
                path=temp_path,
                filename=version.original_filename or temp_path.name,
                media_type=version.media_type or "application/octet-stream",
                source_sha256=version.content_sha256 or "",
                max_pages=self._settings.parse_max_pages,
                enable_ocr=self._settings.parse_enable_ocr,
                ocr_languages=tuple(self._settings.parse_ocr_languages),
            )
            if isinstance(self._parser, DoclingDocumentParser):
                result = await parse_docling_in_subprocess(
                    parse_input,
                    timeout_seconds=self._settings.parse_timeout_seconds,
                )
            else:
                result = await asyncio.wait_for(
                    self._parser.parse(parse_input),
                    timeout=self._settings.parse_timeout_seconds,
                )
            keys = await self._write_artifacts(
                source,
                version,
                artifact,
                result,
                token=token,
            )
            committed = await self._finish(
                session,
                job_id=job_id,
                token=token,
                source=source,
                version=version,
                artifact=artifact,
                result=result,
                keys=keys,
            )
        except DocumentParseError as exc:
            if not exc.retryable:
                raise PermanentJobError(exc.code, str(exc)) from exc
            raise
        except TimeoutError as exc:
            raise RuntimeError("Document parsing timed out") from exc
        finally:
            stop.set()
            if heartbeat is not None:
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if keys is not None and not committed:
                await self._delete_artifacts(keys)
            if temp_path is not None:
                await asyncio.to_thread(temp_path.unlink, missing_ok=True)

    async def _prepare(
        self, session: AsyncSession, *, job_id: UUID, token: ClaimToken
    ) -> tuple[SourceVersion, Source, SourceParseArtifact] | None:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not self._owns(job, token) or job is None or job.source_version_id is None:
                return None
            version = await session.scalar(
                select(SourceVersion).where(SourceVersion.id == job.source_version_id)
            )
            if version is None or version.storage_key is None or version.content_sha256 is None:
                raise PermanentJobError(
                    "source_version_not_ready", "The source version is not ready for parsing."
                )
            source = await session.scalar(
                select(Source).where(
                    Source.id == version.source_id, Source.space_id == job.space_id
                )
            )
            if source is None:
                raise PermanentJobError("source_not_found", "The source was not found.")
            artifact = await session.scalar(
                select(SourceParseArtifact)
                .where(
                    SourceParseArtifact.source_version_id == version.id,
                    SourceParseArtifact.status.in_(
                        [ParseArtifactStatus.QUEUED.value, ParseArtifactStatus.PARSING.value]
                    ),
                )
                .order_by(SourceParseArtifact.revision.desc())
                .with_for_update()
            )
            if artifact is None:
                return None
            now = datetime.now(UTC)
            artifact.status = ParseArtifactStatus.PARSING.value
            artifact.started_at = artifact.started_at or now
            artifact.error_code = None
            artifact.error_message = None
            version.parse_status = ParseStatus.PARSING.value
            job.progress = 20
            await session.flush()
            return version, source, artifact

    async def _download(self, version: SourceVersion, suffix: str) -> Path:
        if version.storage_key is None or version.content_sha256 is None:
            raise PermanentJobError("source_version_not_ready", "Source object is missing.")
        before = await self._storage.stat(version.storage_key)
        if before.size > self._settings.parse_max_source_bytes:
            raise PermanentJobError("parse_source_too_large", "Source exceeds parser size limit.")
        digest = hashlib.sha256()
        size = 0
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        path = Path(handle.name)
        try:
            async for chunk in self._storage.iter_bytes(version.storage_key):
                size += len(chunk)
                if size > self._settings.parse_max_source_bytes:
                    raise PermanentJobError(
                        "parse_source_too_large", "Source exceeds parser size limit."
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.close()
            after = await self._storage.stat(version.storage_key)
            if (
                before.etag != after.etag
                or before.size != after.size
                or size != before.size
                or digest.hexdigest() != version.content_sha256
            ):
                raise PermanentJobError(
                    "source_object_changed", "The immutable source failed its integrity check."
                )
        except BaseException:
            handle.close()
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise
        return path

    async def _write_artifacts(
        self,
        source: Source,
        version: SourceVersion,
        artifact: SourceParseArtifact,
        result: ParseResult,
        *,
        token: ClaimToken,
    ) -> dict[str, StoredObject]:
        canonical = result.document.to_json_bytes()
        prefix = (
            f"{self._settings.parse_artifact_prefix}/{source.space_id}/{source.id}/"
            f"{version.id}/parse/{artifact.revision}/attempts/{token.attempt_id}"
        )
        values = {
            "native": (result.native_json, "application/json", "native.json"),
            "markdown": (result.markdown, "text/markdown", "document.md"),
            "canonical": (canonical, "application/json", "canonical.json"),
        }
        stored: dict[str, StoredObject] = {}
        try:
            for name, (content, media_type, filename) in values.items():
                digest = hashlib.sha256(content).hexdigest()
                obj = await self._storage.put_bytes(
                    key=f"{prefix}/{filename}",
                    content=content,
                    media_type=media_type,
                    content_sha256=digest,
                )
                stored[name] = obj
        except Exception:
            await self._delete_artifacts(stored)
            raise
        return stored

    async def _delete_artifacts(self, keys: dict[str, StoredObject]) -> None:
        for stored in keys.values():
            with suppress(Exception):
                await self._storage.delete(stored.key)

    async def _finish(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        token: ClaimToken,
        source: Source,
        version: SourceVersion,
        artifact: SourceParseArtifact,
        result: ParseResult,
        keys: dict[str, StoredObject],
    ) -> bool:
        async with session.begin():
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None or not self._owns(job, token):
                return False
            attempt = await session.scalar(
                select(JobAttempt)
                .where(
                    JobAttempt.id == token.attempt_id,
                    JobAttempt.job_id == job.id,
                    JobAttempt.attempt_number == token.attempt_number,
                    JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    JobAttempt.lease_expires_at > datetime.now(UTC),
                )
                .with_for_update()
            )
            db_artifact = await session.scalar(
                select(SourceParseArtifact)
                .where(SourceParseArtifact.id == artifact.id)
                .with_for_update()
            )
            db_version = await session.scalar(
                select(SourceVersion).where(SourceVersion.id == version.id).with_for_update()
            )
            if (
                attempt is None
                or db_artifact is None
                or db_version is None
                or job.source_version_id != db_version.id
                or db_version.source_id != source.id
                or db_artifact.source_version_id != db_version.id
                or db_artifact.status != ParseArtifactStatus.PARSING.value
            ):
                return False
            existing = await session.scalar(
                select(SourceSection.id)
                .where(SourceSection.parse_artifact_id == artifact.id)
                .limit(1)
            )
            if existing is None:
                for block in result.document.blocks:
                    session.add(
                        section_from_block(
                            block=block,
                            section_id=uuid4(),
                            space_id=source.space_id,
                            source_id=source.id,
                            version_id=version.id,
                            artifact_id=artifact.id,
                            revision=artifact.revision,
                            content_hash=result.document.content_sha256,
                        )
                    )
            now = datetime.now(UTC)
            db_artifact.parser_name = result.parser_name
            db_artifact.parser_version = result.parser_version
            db_artifact.parser_config = result.parser_config
            db_artifact.native_storage_key = keys["native"].key
            db_artifact.native_sha256 = keys["native"].content_sha256
            db_artifact.markdown_storage_key = keys["markdown"].key
            db_artifact.markdown_sha256 = keys["markdown"].content_sha256
            db_artifact.canonical_storage_key = keys["canonical"].key
            db_artifact.canonical_content_sha256 = result.document.content_sha256
            db_artifact.page_count = result.document.page_count
            db_artifact.warnings = list(result.warnings)
            db_artifact.status = ParseArtifactStatus.READY.value
            db_artifact.completed_at = now
            db_version.current_parse_artifact_id = db_artifact.id
            db_version.parse_status = ParseStatus.READY.value
            job.status = JobStatus.SUCCEEDED.value
            job.progress = 100
            job.retryable = False
            job.error_code = None
            job.error_message = None
            job.finished_at = now
            job.updated_at = now
            attempt.status = JobAttemptStatus.SUCCEEDED.value
            attempt.finished_at = now
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now
        return True

    async def _on_failed(self, session: AsyncSession, *, job: Job, now: datetime) -> None:
        if job.source_version_id is None:
            return
        version = await session.scalar(
            select(SourceVersion).where(SourceVersion.id == job.source_version_id)
        )
        artifact = await session.scalar(
            select(SourceParseArtifact)
            .where(
                SourceParseArtifact.source_version_id == job.source_version_id,
                SourceParseArtifact.status.in_(
                    [ParseArtifactStatus.QUEUED.value, ParseArtifactStatus.PARSING.value]
                ),
            )
            .order_by(SourceParseArtifact.revision.desc())
        )
        if version is not None:
            version.parse_status = ParseStatus.FAILED.value
        if artifact is not None:
            artifact.status = ParseArtifactStatus.FAILED.value
            artifact.error_code = job.error_code
            artifact.error_message = job.error_message
            artifact.completed_at = now

    @staticmethod
    def _owns(job: Job | None, token: ClaimToken) -> bool:
        return bool(
            job is not None
            and job.kind == JobKind.SOURCE_PARSE.value
            and job.status == JobStatus.RUNNING.value
            and job.attempt_count == token.attempt_number
        )

    async def _heartbeat_loop(self, job_id: UUID, token: ClaimToken, stop: asyncio.Event) -> None:
        assert self._heartbeat_sessions is not None
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self._settings.parse_heartbeat_seconds
                )
                return
            except TimeoutError:
                async with self._heartbeat_sessions() as heartbeat_session:
                    if not await self._heartbeat(
                        heartbeat_session, job_id=job_id, token=token
                    ):
                        return


def _source_suffix(version: SourceVersion) -> str:
    filename_suffix = Path(version.original_filename or "").suffix
    if filename_suffix:
        return filename_suffix
    media_suffixes = {
        "application/pdf": ".pdf",
        "text/markdown": ".md",
        "text/plain": ".txt",
        "text/html": ".html",
        "application/xhtml+xml": ".html",
    }
    return media_suffixes.get(version.media_type or "", ".bin")


async def run_source_parse(
    settings: Settings,
    *,
    job_id: UUID,
    celery_task_id: str | None,
    worker_name: str | None,
) -> None:
    engine = create_engine(settings)
    sessions: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    client = Minio(
        settings.s3_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )
    storage = MinioObjectStorage(
        client, client, settings.s3_bucket, settings.s3_public_endpoint
    )
    try:
        async with sessions() as session:
            await SourceParseWorker(
                settings, DoclingDocumentParser(), storage, sessions
            ).run(
                session,
                job_id=job_id,
                celery_task_id=celery_task_id,
                worker_name=worker_name,
            )
    finally:
        await engine.dispose()


def run_source_parse_sync(
    settings: Settings,
    *,
    job_id: UUID,
    celery_task_id: str | None,
    worker_name: str | None,
) -> None:
    asyncio.run(
        run_source_parse(
            settings,
            job_id=job_id,
            celery_task_id=celery_task_id,
            worker_name=worker_name,
        )
    )


async def mark_transient_failure(
    settings: Settings, *, job_id: UUID, token: ClaimToken, message: str
) -> None:
    await mark_job_transient_failure(
        settings,
        job_kind=JobKind.SOURCE_PARSE,
        job_id=job_id,
        token=token,
        message=message,
        error_code="source_parse_transient_failure",
    )


def mark_transient_failure_sync(
    settings: Settings, *, job_id: UUID, token: ClaimToken, message: str
) -> None:
    mark_job_transient_failure_sync(
        settings,
        job_kind=JobKind.SOURCE_PARSE,
        job_id=job_id,
        token=token,
        message=message,
        error_code="source_parse_transient_failure",
    )
