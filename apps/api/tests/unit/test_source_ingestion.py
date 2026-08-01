from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fake_storage import FakeObjectStorage

from knowledge_workbench.application.source_ingestion import (
    SourceIngestionService,
    UploadDeclaration,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    JobStatus,
    KnowledgeSpace,
    ParseStatus,
    ProcessingStatus,
    Source,
    SourceCreateRequest,
    SourceKind,
    SourceStatus,
    SourceVersion,
)


class FakeScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)


class FakeSession:
    def __init__(self, source: Source, version: SourceVersion | None = None) -> None:
        self.source = source
        self.version = version
        self.added: list[object] = []
        self.scalar_values: list[object] = []

    async def get(self, model: object, key: object) -> object | None:
        if model is KnowledgeSpace and key == self.source.space_id:
            return KnowledgeSpace(id=self.source.space_id, slug="space", name="Space")
        return None

    async def scalar(self, statement: object) -> object | None:
        del statement
        if self.scalar_values:
            return self.scalar_values.pop(0)
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        pass


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        s3_presign_ttl_seconds=900,
        max_upload_size_bytes=25 * 1024 * 1024,
    )


def _source() -> Source:
    now = datetime.now(UTC)
    return Source(
        id=uuid4(),
        space_id=uuid4(),
        kind=SourceKind.MARKDOWN.value,
        title="Notes",
        status=SourceStatus.PENDING.value,
        created_at=now,
        updated_at=now,
    )


async def test_create_source_persists_idempotency_result(settings: Settings) -> None:
    source = _source()
    space = KnowledgeSpace(id=source.space_id, slug="space", name="Space")
    storage = FakeObjectStorage()
    session = FakeSession(source)
    session.scalar_values = [space, None]

    created = await SourceIngestionService(storage, settings).create_source(
        session,  # type: ignore[arg-type]
        space_id=source.space_id,
        kind=SourceKind.MARKDOWN,
        title=" Notes ",
        idempotency_key="create-1",
    )

    requests = [value for value in session.added if isinstance(value, SourceCreateRequest)]
    assert created.title == "Notes"
    assert len(requests) == 1
    assert requests[0].source_id == created.id


async def test_create_source_replay_rejects_changed_request(settings: Settings) -> None:
    source = _source()
    space = KnowledgeSpace(id=source.space_id, slug="space", name="Space")
    request = SourceCreateRequest(
        id=uuid4(),
        space_id=source.space_id,
        idempotency_key="create-1",
        request_hash="different",
        source_id=source.id,
    )
    session = FakeSession(source)
    session.scalar_values = [space, request]

    with pytest.raises(AppError) as caught:
        await SourceIngestionService(FakeObjectStorage(), settings).create_source(
            session,  # type: ignore[arg-type]
            space_id=source.space_id,
            kind=SourceKind.MARKDOWN,
            title="Notes",
            idempotency_key="create-1",
        )

    assert caught.value.code == "idempotency_conflict"


def _version(source: Source, content: bytes) -> SourceVersion:
    now = datetime.now(UTC)
    digest = hashlib.sha256(content).hexdigest()
    return SourceVersion(
        id=uuid4(),
        source_id=source.id,
        version_number=1,
        content_sha256=None,
        expected_content_sha256=digest,
        original_filename="notes.md",
        media_type="text/markdown",
        size_bytes=len(content),
        upload_storage_key=f"uploads/{source.id}/notes.md",
        storage_key=None,
        object_etag=None,
        upload_idempotency_key="reserve-1",
        upload_request_hash="b" * 64,
        completion_idempotency_key=None,
        upload_expires_at=now + timedelta(seconds=900),
        completed_at=None,
        processing_status=ProcessingStatus.PENDING.value,
        parse_status=ParseStatus.NOT_STARTED.value,
        created_at=now,
    )


async def test_reserve_upload_uses_900_second_presign_and_immutable_version(
    settings: Settings,
) -> None:
    source = _source()
    storage = FakeObjectStorage()
    session = FakeSession(source)
    session.scalar_values = [source, None, None]
    service = SourceIngestionService(storage, settings)
    content = b"# hello\n"

    reservation = await service.reserve_upload(
        session,  # type: ignore[arg-type]
        space_id=source.space_id,
        source_id=source.id,
        declaration=UploadDeclaration(
            original_filename="notes.md",
            media_type="text/markdown",
            size_bytes=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
        ),
        idempotency_key="reserve-1",
    )

    assert reservation.version.version_number == 1
    assert reservation.version.content_sha256 is None
    assert reservation.upload.headers == {}
    assert reservation.upload.fields["Content-Type"] == "text/markdown"
    assert reservation.upload.fields["key"] == reservation.version.upload_storage_key
    assert reservation.upload.fields["size"] == str(len(content))
    assert reservation.version.upload_expires_at > datetime.now(UTC)


async def test_complete_upload_validates_promotes_and_writes_job_outbox(
    settings: Settings,
) -> None:
    source = _source()
    content = b"# hello\n"
    version = _version(source, content)
    storage = FakeObjectStorage()
    storage.put(version.upload_storage_key, content, version.media_type)
    session = FakeSession(source, version)
    session.scalar_values = [source, version]

    completed = await SourceIngestionService(storage, settings).complete_upload(
        session,  # type: ignore[arg-type]
        space_id=source.space_id,
        source_id=source.id,
        version_id=version.id,
        idempotency_key="complete-1",
    )

    assert completed.version.content_sha256 == hashlib.sha256(content).hexdigest()
    assert completed.version.storage_key is not None
    assert completed.version.parse_status == ParseStatus.NOT_STARTED.value
    assert completed.job.status == JobStatus.QUEUED.value
    assert len(storage.promotions) == 1
    assert len(session.added) == 3


async def test_reservation_replay_uses_remaining_ttl(settings: Settings) -> None:
    source = _source()
    content = b"# hello\n"
    version = _version(source, content)
    version.upload_request_hash = hashlib.sha256(
        b'{"content_sha256":"' + version.expected_content_sha256.encode()
        + b'","filename":"notes.md","media_type":"text/markdown","size_bytes":8}'
    ).hexdigest()
    version.upload_expires_at = datetime.now(UTC) + timedelta(seconds=120)
    storage = FakeObjectStorage()
    session = FakeSession(source, version)
    session.scalar_values = [source, version]

    await SourceIngestionService(storage, settings).reserve_upload(
        session,  # type: ignore[arg-type]
        space_id=source.space_id,
        source_id=source.id,
        declaration=UploadDeclaration(
            original_filename="notes.md",
            media_type="text/markdown",
            size_bytes=len(content),
            content_sha256=version.expected_content_sha256,
        ),
        idempotency_key="reserve-1",
    )

    assert timedelta(seconds=115) < storage.presign_expires[-1] <= timedelta(seconds=120)


async def test_reservation_replay_does_not_reopen_completed_upload(
    settings: Settings,
) -> None:
    source = _source()
    content = b"# hello\n"
    version = _version(source, content)
    version.completed_at = datetime.now(UTC)
    version.upload_request_hash = hashlib.sha256(
        b'{"content_sha256":"' + version.expected_content_sha256.encode()
        + b'","filename":"notes.md","media_type":"text/markdown","size_bytes":8}'
    ).hexdigest()
    session = FakeSession(source, version)
    session.scalar_values = [source, version]

    with pytest.raises(AppError) as caught:
        await SourceIngestionService(FakeObjectStorage(), settings).reserve_upload(
            session,  # type: ignore[arg-type]
            space_id=source.space_id,
            source_id=source.id,
            declaration=UploadDeclaration(
                original_filename="notes.md",
                media_type="text/markdown",
                size_bytes=len(content),
                content_sha256=version.expected_content_sha256,
            ),
            idempotency_key="reserve-1",
        )
    assert caught.value.code == "upload_already_completed"


async def test_complete_rejects_duplicate_content_before_promotion(settings: Settings) -> None:
    source = _source()
    content = b"# hello\n"
    version = _version(source, content)
    storage = FakeObjectStorage()
    storage.put(version.upload_storage_key, content, version.media_type)
    session = FakeSession(source, version)
    session.scalar_values = [source, version, uuid4()]

    with pytest.raises(AppError) as caught:
        await SourceIngestionService(storage, settings).complete_upload(
            session,  # type: ignore[arg-type]
            space_id=source.space_id,
            source_id=source.id,
            version_id=version.id,
            idempotency_key="complete-1",
        )
    assert caught.value.code == "source_content_already_exists"
    assert storage.promotions == []


async def test_complete_recovers_matching_immutable_object_after_expiry(
    settings: Settings,
) -> None:
    source = _source()
    content = b"# hello\n"
    version = _version(source, content)
    version.upload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    suffix = ".md"
    immutable_key = (
        f"sources/{source.space_id}/{source.id}/versions/{version.id}/"
        f"{version.expected_content_sha256}{suffix}"
    )
    storage = FakeObjectStorage()
    storage.put(immutable_key, content, version.media_type, etag="immutable-etag")
    storage.content_sha256[immutable_key] = version.expected_content_sha256
    session = FakeSession(source, version)
    session.scalar_values = [source, version]

    completed = await SourceIngestionService(storage, settings).complete_upload(
        session,  # type: ignore[arg-type]
        space_id=source.space_id,
        source_id=source.id,
        version_id=version.id,
        idempotency_key="complete-recovery",
    )

    assert completed.version.storage_key == immutable_key
    assert completed.version.object_etag == "immutable-etag"
    assert completed.version.completed_at is not None


async def test_complete_deletes_invalid_immutable_destination(settings: Settings) -> None:
    source = _source()
    content = b"# hello\n"
    version = _version(source, content)
    storage = FakeObjectStorage()
    storage.put(version.upload_storage_key, content, version.media_type)
    session = FakeSession(source, version)
    session.scalar_values = [source, version, None]
    original_promote = storage.promote

    async def corrupt_promote(*args, **kwargs):
        promoted = await original_promote(*args, **kwargs)
        storage.put(promoted.key, b"x" * len(content), version.media_type, promoted.etag)
        return promoted

    storage.promote = corrupt_promote  # type: ignore[method-assign]

    with pytest.raises(AppError) as caught:
        await SourceIngestionService(storage, settings).complete_upload(
            session,  # type: ignore[arg-type]
            space_id=source.space_id,
            source_id=source.id,
            version_id=version.id,
            idempotency_key="complete-corrupt",
        )

    assert caught.value.code == "object_promotion_mismatch"
    assert len(storage.deleted) == 1


async def test_fake_storage_rejects_conflicting_immutable_destination() -> None:
    storage = FakeObjectStorage()
    storage.put("upload", b"new", "text/plain")
    storage.put("immutable", b"old", "text/plain")
    storage.content_sha256["immutable"] = hashlib.sha256(b"old").hexdigest()

    with pytest.raises(AppError) as caught:
        await storage.promote(
            "upload",
            "immutable",
            "etag-1",
            hashlib.sha256(b"new").hexdigest(),
        )
    assert caught.value.code == "immutable_object_conflict"


async def test_complete_upload_rejects_checksum_mismatch(settings: Settings) -> None:
    source = _source()
    declared = b"# declared\n"
    uploaded = b"# different\n"
    version = _version(source, declared)
    storage = FakeObjectStorage()
    storage.put(version.upload_storage_key, uploaded, version.media_type)
    version.size_bytes = len(uploaded)
    session = FakeSession(source, version)
    session.scalar_values = [source, version]

    with pytest.raises(AppError) as caught:
        await SourceIngestionService(storage, settings).complete_upload(
            session,  # type: ignore[arg-type]
            space_id=source.space_id,
            source_id=source.id,
            version_id=version.id,
            idempotency_key="complete-1",
        )
    assert caught.value.code == "upload_checksum_mismatch"
