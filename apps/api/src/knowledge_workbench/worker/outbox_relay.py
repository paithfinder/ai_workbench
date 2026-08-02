from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.ports.object_storage import ObjectStorage
from knowledge_workbench.db.models import OutboxEvent
from knowledge_workbench.worker.job_runner import (
    recover_expired_leases,
    recover_orphaned_queued_jobs,
)


class TaskPublisher(Protocol):
    def publish_source_ingest(self, payload: Mapping[str, object]) -> None: ...

    def publish_source_parse(self, payload: Mapping[str, object]) -> None: ...

    async def cleanup_staging(self, storage_key: str) -> None: ...


class CeleryTaskPublisher:
    def __init__(self, storage: ObjectStorage | None = None) -> None:
        self._storage = storage

    def publish_source_ingest(self, payload: Mapping[str, object]) -> None:
        from knowledge_workbench.worker.celery_app import celery_app

        celery_app.send_task(
            "knowledge_workbench.source_ingest",
            kwargs=dict(payload),
            queue="source-ingest",
        )

    def publish_source_parse(self, payload: Mapping[str, object]) -> None:
        from knowledge_workbench.worker.celery_app import celery_app

        celery_app.send_task(
            "knowledge_workbench.source_parse",
            kwargs=dict(payload),
            queue="source-parse",
        )

    async def cleanup_staging(self, storage_key: str) -> None:
        if self._storage is None:
            raise RuntimeError("Object storage was not configured for cleanup")
        await self._storage.delete(storage_key)


async def relay_batch(
    session: AsyncSession,
    publisher: TaskPublisher,
    *,
    batch_size: int,
) -> int:
    now = datetime.now(UTC)
    result = await session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= now)
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    events = list(result)
    for event in events:
        event.attempt_count += 1
        try:
            if event.event_type == "job.source_ingest.requested":
                publisher.publish_source_ingest(event.payload)
            elif event.event_type == "job.source_parse.requested":
                publisher.publish_source_parse(event.payload)
            elif event.event_type == "storage.staging_cleanup.requested":
                storage_key = event.payload.get("storage_key")
                if not isinstance(storage_key, str):
                    raise ValueError("Staging cleanup event has no storage_key")
                await publisher.cleanup_staging(storage_key)
            else:
                raise ValueError(f"Unsupported outbox event type: {event.event_type}")
        except Exception as exc:
            event.last_error = str(exc)[:2000]
            delay_seconds = min(300, 2 ** min(event.attempt_count, 8))
            event.available_at = now + timedelta(seconds=delay_seconds)
        else:
            event.published_at = now
            event.last_error = None
    await session.flush()
    return len(events)


async def run_relay() -> None:
    from minio import Minio

    from knowledge_workbench.config import get_settings
    from knowledge_workbench.db.session import create_engine, create_session_factory
    from knowledge_workbench.infrastructure.storage.minio import MinioObjectStorage

    settings = get_settings()
    internal_client = Minio(
        settings.s3_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )
    storage = MinioObjectStorage(
        internal_client,
        internal_client,
        settings.s3_bucket,
        settings.s3_endpoint,
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    publisher = CeleryTaskPublisher(storage)
    try:
        while True:
            async with session_factory() as session, session.begin():
                await recover_expired_leases(
                    session,
                    batch_size=settings.outbox_batch_size,
                )
                await recover_orphaned_queued_jobs(
                    session,
                    batch_size=settings.outbox_batch_size,
                )
                await relay_batch(
                    session,
                    publisher,
                    batch_size=settings.outbox_batch_size,
                )
            await asyncio.sleep(settings.outbox_relay_interval_seconds)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run_relay())


if __name__ == "__main__":
    main()
