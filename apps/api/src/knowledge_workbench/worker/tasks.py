from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task  # type: ignore[import-untyped]

from knowledge_workbench.config import get_settings
from knowledge_workbench.db.models import JobKind
from knowledge_workbench.worker.celery_app import celery_app
from knowledge_workbench.worker.source_ingest import (
    ActiveJobLeaseError,
    IngestAttemptError,
    mark_transient_failure_sync,
    run_source_ingest_sync,
)

MAX_TASK_RETRIES = 8


def _retry_countdown(retries: int) -> int:
    exponent: int = min(retries + 1, 8)
    return int(min(300, 2**exponent))


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
        raise self.retry(
            exc=exc,
            countdown=exc.retry_after_seconds,
            max_retries=MAX_TASK_RETRIES,
        ) from exc
    except IngestAttemptError as exc:
        retries = request.retries or 0
        if retries >= MAX_TASK_RETRIES:
            mark_transient_failure_sync(
                get_settings(),
                job_id=UUID(job_id),
                token=exc.token,
                message=f"Worker retries exhausted: {exc}",
            )
            return
        raise self.retry(
            exc=exc,
            countdown=_retry_countdown(retries),
            max_retries=MAX_TASK_RETRIES,
        ) from exc
    except Exception as exc:
        retries = request.retries or 0
        if retries >= MAX_TASK_RETRIES:
            raise
        raise self.retry(
            exc=exc,
            countdown=_retry_countdown(retries),
            max_retries=MAX_TASK_RETRIES,
        ) from exc


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="knowledge_workbench.source_parse",
    ignore_result=True,
)
def source_parse(self: Task[Any, Any], *, job_id: str, space_id: str) -> None:
    """Dispatch to the parse worker when that D3 integration module is present."""
    del space_id
    try:
        from knowledge_workbench.worker.job_runner import (
            JobAttemptError as ParseAttemptError,
        )
        from knowledge_workbench.worker.job_runner import (
            release_transient_attempt_sync as release_parse_attempt_sync,
        )
        from knowledge_workbench.worker.source_parse import (
            mark_transient_failure_sync as mark_parse_failure_sync,
        )
        from knowledge_workbench.worker.source_parse import run_source_parse_sync
    except ImportError as exc:
        raise RuntimeError("The source_parse worker integration is not available") from exc

    request = self.request
    delivery_info = request.delivery_info or {}
    try:
        run_source_parse_sync(
            get_settings(),
            job_id=UUID(job_id),
            celery_task_id=request.id,
            worker_name=delivery_info.get("consumer_tag"),
        )
    except ActiveJobLeaseError as exc:
        raise self.retry(
            exc=exc,
            countdown=exc.retry_after_seconds,
            max_retries=MAX_TASK_RETRIES,
        ) from exc
    except ParseAttemptError as exc:
        retries = request.retries or 0
        if retries >= MAX_TASK_RETRIES:
            mark_parse_failure_sync(
                get_settings(),
                job_id=UUID(job_id),
                token=exc.token,
                message=f"Worker retries exhausted: {exc}",
            )
            return
        release_parse_attempt_sync(
            get_settings(),
            job_kind=JobKind.SOURCE_PARSE,
            job_id=UUID(job_id),
            token=exc.token,
            message=str(exc),
        )
        raise self.retry(
            exc=exc,
            countdown=_retry_countdown(retries),
            max_retries=MAX_TASK_RETRIES,
        ) from exc
    except Exception as exc:
        retries = request.retries or 0
        if retries >= MAX_TASK_RETRIES:
            raise
        raise self.retry(
            exc=exc,
            countdown=_retry_countdown(retries),
            max_retries=MAX_TASK_RETRIES,
        ) from exc
