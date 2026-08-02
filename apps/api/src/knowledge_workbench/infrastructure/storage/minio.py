from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from minio import Minio
from minio.commonconfig import CopySource
from minio.datatypes import PostPolicy
from minio.error import S3Error

from knowledge_workbench.application.ports.object_storage import (
    ObjectStorage,
    PresignedUpload,
    StoredObject,
)
from knowledge_workbench.core.errors import AppError


class MinioObjectStorage(ObjectStorage):
    def __init__(
        self,
        internal_client: Minio,
        signing_client: Minio,
        bucket: str,
        public_endpoint: str,
    ) -> None:
        self._internal = internal_client
        self._signing = signing_client
        self._bucket = bucket
        self._public_endpoint = public_endpoint

    async def presign_post(
        self,
        key: str,
        media_type: str,
        size_bytes: int,
        expires: timedelta,
    ) -> PresignedUpload:
        policy = PostPolicy(self._bucket, datetime.now(UTC) + expires)
        policy.add_equals_condition("key", key)
        policy.add_equals_condition("Content-Type", media_type)
        policy.add_content_length_range_condition(size_bytes, size_bytes)
        try:
            signed_fields = await asyncio.to_thread(
                self._signing.presigned_post_policy, policy
            )
        except S3Error as exc:
            raise _storage_error() from exc
        fields = {
            **signed_fields,
            "key": key,
            "Content-Type": media_type,
        }
        public = urlsplit(self._public_endpoint)
        url = urlunsplit(
            (public.scheme, public.netloc, f"/{self._bucket}", "", "")
        )
        return PresignedUpload(url=url, headers={}, fields=fields)

    async def stat(self, key: str) -> StoredObject:
        try:
            value = await asyncio.to_thread(self._internal.stat_object, self._bucket, key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise AppError(
                    "uploaded_object_missing",
                    "The uploaded file could not be found. Upload it again and retry.",
                    status_code=409,
                ) from exc
            raise _storage_error() from exc
        if value.size is None or value.etag is None:
            raise _storage_error()
        return StoredObject(
            key=key,
            size=value.size,
            etag=value.etag,
            media_type=(value.content_type or "").split(";", 1)[0].strip().lower(),
            content_sha256=_content_sha256(value.metadata),
        )

    async def iter_bytes(self, key: str) -> AsyncIterator[bytes]:
        try:
            response = await asyncio.to_thread(self._internal.get_object, self._bucket, key)
        except S3Error as exc:
            raise _storage_error() from exc
        try:
            while chunk := await asyncio.to_thread(response.read, 1024 * 1024):
                yield chunk
        finally:
            response.close()
            response.release_conn()

    async def put_bytes(
        self,
        *,
        key: str,
        content: bytes,
        media_type: str,
        content_sha256: str,
    ) -> StoredObject:
        try:
            existing = await asyncio.to_thread(
                self._internal.stat_object, self._bucket, key
            )
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise _storage_error() from exc
        else:
            existing_sha256 = _content_sha256(existing.metadata)
            if (
                existing.size != len(content)
                or existing_sha256 != content_sha256
                or existing.etag is None
            ):
                raise AppError(
                    "immutable_object_conflict",
                    "The immutable object key already contains different content.",
                    status_code=409,
                )
            return StoredObject(
                key=key,
                size=existing.size,
                etag=existing.etag,
                media_type=(existing.content_type or "").split(";", 1)[0].strip().lower(),
                content_sha256=existing_sha256,
            )
        try:
            await asyncio.to_thread(
                self._internal.put_object,
                self._bucket,
                key,
                io.BytesIO(content),
                len(content),
                content_type=media_type,
                metadata={"Content-Sha256": content_sha256},
            )
        except S3Error as exc:
            raise _storage_error() from exc
        stored = await self.stat(key)
        if stored.size != len(content) or stored.content_sha256 != content_sha256:
            raise _storage_error()
        return stored

    async def promote(
        self,
        source_key: str,
        destination_key: str,
        expected_etag: str,
        content_sha256: str,
    ) -> StoredObject:
        try:
            existing = await asyncio.to_thread(
                self._internal.stat_object, self._bucket, destination_key
            )
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                raise _storage_error() from exc
        else:
            if existing.size is None or existing.etag is None:
                raise _storage_error()
            existing_sha256 = _content_sha256(existing.metadata)
            if existing_sha256 != content_sha256:
                raise AppError(
                    "immutable_object_conflict",
                    "The immutable object key already contains different content.",
                    status_code=409,
                )
            return StoredObject(
                key=destination_key,
                size=existing.size,
                etag=existing.etag,
                media_type=(existing.content_type or "").split(";", 1)[0].strip().lower(),
                content_sha256=existing_sha256,
            )

        try:
            await asyncio.to_thread(
                self._internal.copy_object,
                self._bucket,
                destination_key,
                CopySource(self._bucket, source_key, match_etag=expected_etag),
                metadata={
                    "Content-Type": (await self.stat(source_key)).media_type,
                    "X-Amz-Meta-Content-Sha256": content_sha256,
                },
                metadata_directive="REPLACE",
            )
        except S3Error as exc:
            raise _storage_error() from exc
        return await self.stat(destination_key)

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._internal.remove_object, self._bucket, key)
        except S3Error as exc:
            raise _storage_error() from exc

    def _public_url(self, signed_url: str) -> str:
        public = urlsplit(self._public_endpoint)
        signed = urlsplit(signed_url)
        return urlunsplit(
            (public.scheme, public.netloc, signed.path, signed.query, signed.fragment)
        )


def _content_sha256(metadata: Mapping[str, str] | None) -> str | None:
    if metadata is None:
        return None
    for key, value in metadata.items():
        if key.lower() in {"x-amz-meta-content-sha256", "content-sha256"}:
            return value.lower()
    return None


def _storage_error() -> AppError:
    return AppError(
        "object_storage_unavailable",
        "Object storage is temporarily unavailable. Please retry.",
        status_code=503,
    )
