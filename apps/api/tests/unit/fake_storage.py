from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta

from knowledge_workbench.application.ports.object_storage import PresignedUpload, StoredObject
from knowledge_workbench.core.errors import AppError


class FakeObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.content_sha256: dict[str, str] = {}
        self.promotions: list[tuple[str, str]] = []
        self.presign_expires: list[timedelta] = []
        self.deleted: list[str] = []

    def put(self, key: str, content: bytes, media_type: str, etag: str = "etag-1") -> None:
        self.objects[key] = (content, media_type, etag)

    async def presign_post(
        self, key: str, media_type: str, size_bytes: int, expires: timedelta
    ) -> PresignedUpload:
        self.presign_expires.append(expires)
        return PresignedUpload(
            url="https://storage.test/bucket",
            headers={},
            fields={"key": key, "Content-Type": media_type, "size": str(size_bytes)},
        )

    async def stat(self, key: str) -> StoredObject:
        try:
            content, media_type, etag = self.objects[key]
        except KeyError as exc:
            raise AppError(
                "uploaded_object_missing", "Object is missing.", status_code=409
            ) from exc
        return StoredObject(
            key=key,
            size=len(content),
            etag=etag,
            media_type=media_type,
            content_sha256=self.content_sha256.get(key),
        )

    async def iter_bytes(self, key: str) -> AsyncIterator[bytes]:
        content = self.objects[key][0]
        for offset in range(0, len(content), 7):
            yield content[offset : offset + 7]

    async def put_bytes(
        self,
        *,
        key: str,
        content: bytes,
        media_type: str,
        content_sha256: str,
    ) -> StoredObject:
        if hashlib.sha256(content).hexdigest() != content_sha256:
            raise AppError("checksum_mismatch", "Checksum mismatch.", status_code=422)
        if key in self.objects and self.content_sha256.get(key) != content_sha256:
            raise AppError(
                "immutable_object_conflict", "Immutable content differs.", status_code=409
            )
        self.objects.setdefault(key, (content, media_type, f"etag-{len(self.objects) + 1}"))
        self.content_sha256[key] = content_sha256
        return await self.stat(key)

    async def promote(
        self,
        source_key: str,
        destination_key: str,
        expected_etag: str,
        content_sha256: str,
    ) -> StoredObject:
        content, media_type, etag = self.objects[source_key]
        if etag != expected_etag:
            raise AppError("etag_mismatch", "ETag mismatch.", status_code=409)
        if destination_key in self.objects:
            if self.content_sha256.get(destination_key) != content_sha256:
                raise AppError(
                    "immutable_object_conflict", "Immutable content differs.", status_code=409
                )
        else:
            self.objects[destination_key] = (content, media_type, etag)
            self.content_sha256[destination_key] = content_sha256
        self.promotions.append((source_key, destination_key))
        return await self.stat(destination_key)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)
