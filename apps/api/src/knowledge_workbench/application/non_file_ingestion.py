from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from knowledge_workbench.application.ports.object_storage import ObjectStorage, StoredObject
from knowledge_workbench.application.ports.web_fetcher import WebFetchResult
from knowledge_workbench.core.errors import AppError


@dataclass(frozen=True, slots=True)
class ValidatedPastedText:
    content: bytes
    content_sha256: str
    size_bytes: int
    media_type: str = "text/plain"


@dataclass(frozen=True, slots=True)
class NonFileIngestionRecord:
    source_id: UUID
    version_id: UUID
    job_id: UUID
    storage_key: str
    request_hash: str


@dataclass(frozen=True, slots=True)
class NonFileIngestionClaim:
    request_hash: str
    lease_token: UUID | None
    existing_record: NonFileIngestionRecord | None = None


@dataclass(frozen=True, slots=True)
class PreparedPastedText:
    space_id: UUID
    title: str
    idempotency_key: str
    validated: ValidatedPastedText
    claim: NonFileIngestionClaim


@dataclass(frozen=True, slots=True)
class PreparedWebSource:
    space_id: UUID
    title: str
    idempotency_key: str
    requested_url: str
    claim: NonFileIngestionClaim


@dataclass(frozen=True, slots=True)
class PastedTextIngestionRequest:
    space_id: UUID
    title: str
    idempotency_key: str
    content_sha256: str
    size_bytes: int
    storage_key: str
    object_etag: str
    request_hash: str
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class WebSnapshotIngestionRequest:
    space_id: UUID
    title: str
    idempotency_key: str
    requested_url: str
    final_url: str
    fetched_at: datetime
    media_type: str
    headers: dict[str, str]
    content_sha256: str
    size_bytes: int
    storage_key: str
    object_etag: str
    request_hash: str
    lease_token: UUID


class NonFileIngestionRepository(Protocol):
    """Transactional persistence for idempotent non-file acquisition.

    claim() must validate the space and reserve the idempotency key before any network or
    object-storage side effect. Finalization must fence on lease_token and atomically append the
    Source, Version, ingest Job, and Outbox event.
    """

    async def claim(
        self,
        *,
        space_id: UUID,
        idempotency_key: str,
        request_hash: str,
    ) -> NonFileIngestionClaim: ...

    async def finalize_pasted_text(
        self, request: PastedTextIngestionRequest
    ) -> NonFileIngestionRecord: ...

    async def finalize_web_snapshot(
        self, request: WebSnapshotIngestionRequest
    ) -> NonFileIngestionRecord: ...

    async def release_claim(
        self,
        *,
        space_id: UUID,
        idempotency_key: str,
        lease_token: UUID,
    ) -> None: ...


class NonFileIngestionService:
    def __init__(
        self,
        storage: ObjectStorage,
        repository: NonFileIngestionRepository,
        *,
        max_pasted_text_bytes: int,
        max_title_characters: int = 500,
    ) -> None:
        if max_pasted_text_bytes <= 0 or max_title_characters <= 0:
            raise ValueError("non-file ingestion limits must be positive")
        self._storage = storage
        self._repository = repository
        self._max_pasted_text_bytes = max_pasted_text_bytes
        self._max_title_characters = max_title_characters

    async def prepare_pasted_text(
        self,
        *,
        space_id: UUID,
        title: str,
        text: str,
        idempotency_key: str,
    ) -> PreparedPastedText:
        normalized_title = _validate_title(title, self._max_title_characters)
        key = validate_idempotency_key(idempotency_key)
        validated = validate_pasted_text(text, max_size_bytes=self._max_pasted_text_bytes)
        request_hash = _request_hash(
            {
                "kind": "pasted_text",
                "title": normalized_title,
                "content_sha256": validated.content_sha256,
            }
        )
        claim = await self._repository.claim(
            space_id=space_id,
            idempotency_key=key,
            request_hash=request_hash,
        )
        return PreparedPastedText(
            space_id=space_id,
            title=normalized_title,
            idempotency_key=key,
            validated=validated,
            claim=claim,
        )

    async def stage_pasted_text(
        self, prepared: PreparedPastedText
    ) -> PastedTextIngestionRequest:
        lease_token = _require_new_claim(prepared.claim)
        storage_key = (
            f"sources/{prepared.space_id}/acquisitions/pasted-text/"
            f"{lease_token}/{prepared.validated.content_sha256}.txt"
        )
        stored = await self._store_immutable(
            key=storage_key,
            content=prepared.validated.content,
            media_type=prepared.validated.media_type,
            content_sha256=prepared.validated.content_sha256,
        )
        return PastedTextIngestionRequest(
            space_id=prepared.space_id,
            title=prepared.title,
            idempotency_key=prepared.idempotency_key,
            content_sha256=prepared.validated.content_sha256,
            size_bytes=prepared.validated.size_bytes,
            storage_key=storage_key,
            object_etag=stored.etag,
            request_hash=prepared.claim.request_hash,
            lease_token=lease_token,
        )

    async def prepare_web_source(
        self,
        *,
        space_id: UUID,
        title: str,
        requested_url: str,
        idempotency_key: str,
    ) -> PreparedWebSource:
        normalized_title = _validate_title(title, self._max_title_characters)
        key = validate_idempotency_key(idempotency_key)
        request_hash = _request_hash(
            {
                "kind": "web_fetch",
                "title": normalized_title,
                "requested_url": requested_url,
            }
        )
        claim = await self._repository.claim(
            space_id=space_id,
            idempotency_key=key,
            request_hash=request_hash,
        )
        return PreparedWebSource(
            space_id=space_id,
            title=normalized_title,
            idempotency_key=key,
            requested_url=requested_url,
            claim=claim,
        )

    async def stage_web_snapshot(
        self,
        *,
        prepared: PreparedWebSource,
        fetched: WebFetchResult,
    ) -> WebSnapshotIngestionRequest:
        lease_token = _require_new_claim(prepared.claim)
        if fetched.requested_url != prepared.requested_url:
            raise AppError(
                "invalid_web_snapshot",
                "The fetched web snapshot does not match the reserved URL.",
                status_code=422,
            )
        if not fetched.body or fetched.content_sha256 != hashlib.sha256(fetched.body).hexdigest():
            raise AppError(
                "invalid_web_snapshot",
                "The fetched web snapshot is empty or failed its integrity check.",
                status_code=422,
            )
        suffix = ".txt" if fetched.media_type == "text/plain" else ".html"
        storage_key = (
            f"sources/{prepared.space_id}/acquisitions/web/"
            f"{lease_token}/{fetched.content_sha256}{suffix}"
        )
        stored = await self._store_immutable(
            key=storage_key,
            content=fetched.body,
            media_type=fetched.media_type,
            content_sha256=fetched.content_sha256,
        )
        return WebSnapshotIngestionRequest(
            space_id=prepared.space_id,
            title=prepared.title,
            idempotency_key=prepared.idempotency_key,
            requested_url=fetched.requested_url,
            final_url=fetched.final_url,
            fetched_at=fetched.fetched_at,
            media_type=fetched.media_type,
            headers=dict(fetched.headers),
            content_sha256=fetched.content_sha256,
            size_bytes=len(fetched.body),
            storage_key=storage_key,
            object_etag=stored.etag,
            request_hash=prepared.claim.request_hash,
            lease_token=lease_token,
        )

    async def discard_staged(self, storage_key: str) -> None:
        await self._storage.delete(storage_key)

    async def _store_immutable(
        self, *, key: str, content: bytes, media_type: str, content_sha256: str
    ) -> StoredObject:
        stored = await self._storage.put_bytes(
            key=key,
            content=content,
            media_type=media_type,
            content_sha256=content_sha256,
        )
        if (
            stored.key != key
            or stored.size != len(content)
            or stored.media_type != media_type
            or stored.content_sha256 != content_sha256
        ):
            raise AppError(
                "immutable_object_write_mismatch",
                "The immutable source object failed its integrity check.",
                status_code=503,
            )
        return stored


def validate_pasted_text(text: str, *, max_size_bytes: int) -> ValidatedPastedText:
    if not isinstance(text, str):
        raise AppError(
            "invalid_pasted_text",
            "Pasted text must be a UTF-8 string.",
            status_code=422,
        )
    try:
        content = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AppError(
            "invalid_pasted_text_utf8",
            "Pasted text must contain valid Unicode encodable as UTF-8.",
            status_code=422,
        ) from exc
    if not text.strip():
        raise AppError("empty_pasted_text", "Pasted text cannot be empty.", status_code=422)
    if len(content) > max_size_bytes:
        raise AppError(
            "pasted_text_too_large",
            "Pasted text exceeds the configured size limit.",
            status_code=413,
        )
    if "\x00" in text:
        raise AppError(
            "invalid_pasted_text_control",
            "Pasted text cannot contain NUL characters.",
            status_code=422,
        )
    forbidden = [
        index
        for index, character in enumerate(text)
        if unicodedata.category(character) in {"Cc", "Cf"}
        and character not in {"\n", "\r", "\t"}
    ]
    if forbidden:
        raise AppError(
            "invalid_pasted_text_control",
            "Pasted text contains unsupported control characters.",
            status_code=422,
            details=[{"offset": forbidden[0]}],
        )
    return ValidatedPastedText(
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str):
        raise AppError(
            "invalid_idempotency_key",
            "Idempotency-Key must be a string.",
            status_code=422,
        )
    key = value.strip()
    if not key or len(key) > 255 or any(ord(character) < 32 for character in key):
        raise AppError(
            "invalid_idempotency_key",
            "Idempotency-Key must contain 1 to 255 printable characters.",
            status_code=422,
        )
    return key


def _validate_title(value: str, max_characters: int) -> str:
    title = value.strip()
    if not title or len(title) > max_characters or any(ord(character) < 32 for character in title):
        raise AppError("invalid_source_title", "Source title is not valid.", status_code=422)
    return title


def _request_hash(payload: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _require_new_claim(claim: NonFileIngestionClaim) -> UUID:
    if claim.existing_record is not None or claim.lease_token is None:
        raise AppError(
            "non_file_ingestion_inconsistent",
            "The non-file ingestion claim cannot stage new content.",
            status_code=500,
        )
    return claim.lease_token
