from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task  # type: ignore[import-untyped]

from knowledge_workbench.config import get_settings
from knowledge_workbench.worker.celery_app import celery_app
from knowledge_workbench.worker.source_ingest import (
    ActiveJobLeaseError,
    IngestAttemptError,
    mark_transient_failure_sync,
    run_source_ingest_sync,
)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="knowledge_workbench.source_ingest",
    ignore_result=True,
)
def source_ingest(self: Task[Any, Any], *, job_id: str, space_id: str) -> None:
    del space_id  # The worker revalidates the job's authoritative space in PostgreSQL.
    request = self.request
    delivery_info = request.delivery_info or {}
    try:
        run_source_ingest_sync(
            get_settings(),
            job_id=UUID(job_id),
            celery_task_id=request.id,
            worker_name=delivery_info.get("consumer_tag"),
        )
    except ActiveJobLeaseError as exc:
        raise self.retry(exc=exc, countdown=exc.retry_after_seconds, max_retries=8) from exc
    except IngestAttemptError as exc:
        retries = request.retries or 0
        if retries >= 8:
            mark_transient_failure_sync(
                get_settings(),
                job_id=UUID(job_id),
                token=exc.token,
                message=f"Worker retries exhausted: {exc}",
            )
            return
        countdown = min(300, 2 ** min(retries + 1, 8))
        raise self.retry(exc=exc, countdown=countdown, max_retries=8) from exc
    except Exception as exc:
        retries = request.retries or 0
        if retries >= 8:
            raise
        countdown = min(300, 2 ** min(retries + 1, 8))
        raise self.retry(exc=exc, countdown=countdown, max_retries=8) from exc
