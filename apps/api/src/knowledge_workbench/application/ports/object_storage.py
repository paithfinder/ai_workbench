from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    url: str
    headers: dict[str, str]
    fields: dict[str, str]


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size: int
    etag: str
    media_type: str
    content_sha256: str | None = None


class ObjectStorage(Protocol):
    async def presign_post(
        self,
        key: str,
        media_type: str,
        size_bytes: int,
        expires: timedelta,
    ) -> PresignedUpload: ...

    async def stat(self, key: str) -> StoredObject: ...

    def iter_bytes(self, key: str) -> AsyncIterator[bytes]: ...

    async def promote(
        self,
        source_key: str,
        destination_key: str,
        expected_etag: str,
        content_sha256: str,
    ) -> StoredObject: ...

    async def delete(self, key: str) -> None: ...
