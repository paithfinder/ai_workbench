from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from knowledge_workbench.application.non_file_ingestion import (
    NonFileIngestionClaim,
    NonFileIngestionRecord,
    NonFileIngestionService,
    PastedTextIngestionRequest,
    WebSnapshotIngestionRequest,
    validate_pasted_text,
)
from knowledge_workbench.application.ports.object_storage import (
    PresignedUpload,
    StoredObject,
)
from knowledge_workbench.application.ports.web_fetcher import WebFetchResult
from knowledge_workbench.core.errors import AppError


class FakeImmutableStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.writes = 0
        self.deletes: list[str] = []

    async def presign_post(
        self,
        key: str,
        media_type: str,
        size_bytes: int,
        expires: timedelta,
    ) -> PresignedUpload:
        raise NotImplementedError

    async def stat(self, key: str) -> StoredObject:
        raise NotImplementedError

    async def promote(
        self,
        source_key: str,
        destination_key: str,
        expected_etag: str,
        content_sha256: str,
    ) -> StoredObject:
        raise NotImplementedError

    def iter_bytes(self, key: str) -> AsyncIterator[bytes]:
        raise NotImplementedError

    async def put_bytes(
        self,
        *,
        key: str,
        content: bytes,
        media_type: str,
        content_sha256: str,
    ) -> StoredObject:
        self.writes += 1
        existing = self.objects.get(key)
        value = (content, media_type, content_sha256)
        if existing is not None and existing != value:
            raise AppError("immutable_object_conflict", "different", status_code=409)
        self.objects[key] = value
        return StoredObject(
            key=key,
            size=len(content),
            etag=content_sha256[:32],
            media_type=media_type,
            content_sha256=content_sha256,
        )

    async def delete(self, key: str) -> None:
        self.deletes.append(key)
        self.objects.pop(key, None)


class FakeRepository:
    def __init__(self) -> None:
        self.requests: dict[tuple[UUID, str], tuple[str, NonFileIngestionRecord | UUID]] = {}
        self.pasted_calls: list[PastedTextIngestionRequest] = []
        self.web_calls: list[WebSnapshotIngestionRequest] = []

    async def claim(
        self,
        *,
        space_id: UUID,
        idempotency_key: str,
        request_hash: str,
    ) -> NonFileIngestionClaim:
        identity = (space_id, idempotency_key)
        existing = self.requests.get(identity)
        if existing is not None:
            if existing[0] != request_hash:
                raise AppError("idempotency_conflict", "different body", status_code=409)
            if isinstance(existing[1], NonFileIngestionRecord):
                return NonFileIngestionClaim(request_hash, None, existing[1])
            raise AppError("source_creation_in_progress", "running", status_code=409)
        lease_token = uuid4()
        self.requests[identity] = (request_hash, lease_token)
        return NonFileIngestionClaim(request_hash, lease_token)

    async def finalize_pasted_text(
        self, request: PastedTextIngestionRequest
    ) -> NonFileIngestionRecord:
        self.pasted_calls.append(request)
        return self._finalize(
            request.space_id,
            request.idempotency_key,
            request.request_hash,
            request.storage_key,
            request.lease_token,
        )

    async def finalize_web_snapshot(
        self, request: WebSnapshotIngestionRequest
    ) -> NonFileIngestionRecord:
        self.web_calls.append(request)
        return self._finalize(
            request.space_id,
            request.idempotency_key,
            request.request_hash,
            request.storage_key,
            request.lease_token,
        )

    async def release_claim(
        self,
        *,
        space_id: UUID,
        idempotency_key: str,
        lease_token: UUID,
    ) -> None:
        identity = (space_id, idempotency_key)
        existing = self.requests.get(identity)
        if existing is not None and existing[1] == lease_token:
            del self.requests[identity]

    def _finalize(
        self,
        space_id: UUID,
        idempotency_key: str,
        request_hash: str,
        storage_key: str,
        lease_token: UUID,
    ) -> NonFileIngestionRecord:
        identity = (space_id, idempotency_key)
        existing = self.requests[identity]
        if existing != (request_hash, lease_token):
            raise AppError("non_file_ingestion_claim_lost", "lost", status_code=409)
        record = NonFileIngestionRecord(
            source_id=uuid4(),
            version_id=uuid4(),
            job_id=uuid4(),
            storage_key=storage_key,
            request_hash=request_hash,
        )
        self.requests[identity] = (request_hash, record)
        return record


@pytest.mark.parametrize("text", ["", "   \n\t", "\x00", "hello\x07world", "hello​world"])
def test_pasted_text_rejects_empty_nul_and_control_text(text: str) -> None:
    with pytest.raises(AppError) as caught:
        validate_pasted_text(text, max_size_bytes=100)

    assert caught.value.status_code == 422


def test_pasted_text_rejects_unencodable_surrogate() -> None:
    with pytest.raises(AppError) as caught:
        validate_pasted_text("bad \ud800", max_size_bytes=100)

    assert caught.value.code == "invalid_pasted_text_utf8"


def test_pasted_text_uses_utf8_byte_limit_and_server_hash() -> None:
    with pytest.raises(AppError) as caught:
        validate_pasted_text("知识", max_size_bytes=5)
    assert caught.value.code == "pasted_text_too_large"
    assert caught.value.status_code == 413

    result = validate_pasted_text("知识", max_size_bytes=6)
    assert result.content == "知识".encode()
    assert result.size_bytes == 6
    assert result.content_sha256 == hashlib.sha256(result.content).hexdigest()


async def test_pasted_text_replay_skips_second_object_write() -> None:
    storage = FakeImmutableStorage()
    repository = FakeRepository()
    service = NonFileIngestionService(
        storage, repository, max_pasted_text_bytes=1024
    )
    space_id = uuid4()

    prepared = await service.prepare_pasted_text(
        space_id=space_id,
        title=" Notes ",
        text="Line one\nLine two",
        idempotency_key="paste-1",
    )
    staged = await service.stage_pasted_text(prepared)
    first = await repository.finalize_pasted_text(staged)
    replay = await service.prepare_pasted_text(
        space_id=space_id,
        title=" Notes ",
        text="Line one\nLine two",
        idempotency_key="paste-1",
    )

    assert replay.claim.existing_record == first
    assert storage.writes == 1
    request = repository.pasted_calls[0]
    assert request.title == "Notes"
    assert request.content_sha256 == hashlib.sha256(b"Line one\nLine two").hexdigest()
    assert f"/{request.lease_token}/" in request.storage_key
    assert request.storage_key.endswith(f"/{request.content_sha256}.txt")


async def test_pasted_text_conflict_happens_before_object_write() -> None:
    storage = FakeImmutableStorage()
    repository = FakeRepository()
    service = NonFileIngestionService(storage, repository, max_pasted_text_bytes=1024)
    space_id = uuid4()
    first = await service.prepare_pasted_text(
        space_id=space_id,
        title="Notes",
        text="first",
        idempotency_key="paste-1",
    )
    staged = await service.stage_pasted_text(first)
    await repository.finalize_pasted_text(staged)

    with pytest.raises(AppError) as caught:
        await service.prepare_pasted_text(
            space_id=space_id,
            title="Notes",
            text="second",
            idempotency_key="paste-1",
        )

    assert caught.value.code == "idempotency_conflict"
    assert storage.writes == 1


async def test_web_snapshot_preserves_metadata_and_uses_claim_scoped_key() -> None:
    storage = FakeImmutableStorage()
    repository = FakeRepository()
    service = NonFileIngestionService(storage, repository, max_pasted_text_bytes=1024)
    body = b"<html><body>snapshot</body></html>"
    fetched = WebFetchResult(
        requested_url="https://example.com/start",
        final_url="https://www.example.com/article",
        fetched_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        body=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        media_type="text/html",
        headers={"content-type": "text/html", "etag": '"v1"'},
        status_code=200,
    )
    prepared = await service.prepare_web_source(
        space_id=uuid4(),
        title="Article",
        requested_url=fetched.requested_url,
        idempotency_key="web-1",
    )

    request = await service.stage_web_snapshot(prepared=prepared, fetched=fetched)

    assert storage.objects[request.storage_key][0] == body
    assert request.final_url == fetched.final_url
    assert request.fetched_at == fetched.fetched_at
    assert request.headers == fetched.headers
    assert f"/{request.lease_token}/" in request.storage_key
    assert request.storage_key.endswith(f"/{fetched.content_sha256}.html")


async def test_web_replay_and_conflict_need_no_fetch_or_storage_side_effect() -> None:
    storage = FakeImmutableStorage()
    repository = FakeRepository()
    service = NonFileIngestionService(storage, repository, max_pasted_text_bytes=1024)
    space_id = uuid4()
    requested_url = "https://example.com"
    prepared = await service.prepare_web_source(
        space_id=space_id,
        title="Page",
        requested_url=requested_url,
        idempotency_key="web-1",
    )
    body = b"version 1"
    fetched = WebFetchResult(
        requested_url=requested_url,
        final_url="https://cdn.example.com/final",
        fetched_at=datetime.now(UTC),
        body=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        media_type="text/plain",
        headers={},
        status_code=200,
    )
    staged = await service.stage_web_snapshot(prepared=prepared, fetched=fetched)
    first = await repository.finalize_web_snapshot(staged)

    replay = await service.prepare_web_source(
        space_id=space_id,
        title="Page",
        requested_url=requested_url,
        idempotency_key="web-1",
    )
    assert replay.claim.existing_record == first
    assert storage.writes == 1

    with pytest.raises(AppError) as caught:
        await service.prepare_web_source(
            space_id=space_id,
            title="Page",
            requested_url="https://other.example.com",
            idempotency_key="web-1",
        )
    assert caught.value.code == "idempotency_conflict"
    assert storage.writes == 1


async def test_discard_staged_removes_claim_scoped_object() -> None:
    storage = FakeImmutableStorage()
    service = NonFileIngestionService(
        storage, FakeRepository(), max_pasted_text_bytes=1024
    )
    prepared = await service.prepare_pasted_text(
        space_id=uuid4(),
        title="Notes",
        text="content",
        idempotency_key="paste-1",
    )
    staged = await service.stage_pasted_text(prepared)

    await service.discard_staged(staged.storage_key)

    assert storage.deletes == [staged.storage_key]
    assert staged.storage_key not in storage.objects
