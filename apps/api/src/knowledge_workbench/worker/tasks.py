from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task  # type: ignore[import-untyped]

from knowledge_workbench.config import get_settings
from knowledge_workbench.db.models import JobKind
from knowledge_workbench.worker.celery_app import celery_app
from knowledge_workbench.worker.job_runner import ActiveJobLeaseError
from knowledge_workbench.worker.source_ingest import (
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
    del space_id
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
            exc=exc, countdown=exc.retry_after_seconds, max_retries=MAX_TASK_RETRIES
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


def _run_reliable_task(
    self: Task[Any, Any], *, job_id: str, job_kind: JobKind
) -> None:
    from knowledge_workbench.worker.job_runner import JobAttemptError
    from knowledge_workbench.worker.job_runner import (
        release_transient_attempt_sync as release_attempt_sync,
    )

    if job_kind == JobKind.SOURCE_PARSE:
        from knowledge_workbench.worker.source_parse import (
            mark_transient_failure_sync as mark_failure_sync,
        )
        from knowledge_workbench.worker.source_parse import run_source_parse_sync as run_sync
    elif job_kind == JobKind.SOURCE_EXTRACT:
        from knowledge_workbench.worker.source_extract import (
            mark_transient_failure_sync as mark_failure_sync,
        )
        from knowledge_workbench.worker.source_extract import run_source_extract_sync as run_sync
    else:
        raise RuntimeError(f"Unsupported reliable task kind: {job_kind}")

    request = self.request
    delivery_info = request.delivery_info or {}
    try:
        run_sync(
            get_settings(),
            job_id=UUID(job_id),
            celery_task_id=request.id,
            worker_name=delivery_info.get("consumer_tag"),
        )
    except ActiveJobLeaseError as exc:
        raise self.retry(
            exc=exc, countdown=exc.retry_after_seconds, max_retries=MAX_TASK_RETRIES
        ) from exc
    except JobAttemptError as exc:
        retries = request.retries or 0
        if retries >= MAX_TASK_RETRIES:
            mark_failure_sync(
                get_settings(),
                job_id=UUID(job_id),
                token=exc.token,
                message=f"Worker retries exhausted: {exc}",
            )
            return
        release_attempt_sync(
            get_settings(),
            job_kind=job_kind,
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


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="knowledge_workbench.source_parse",
    ignore_result=True,
)
def source_parse(self: Task[Any, Any], *, job_id: str, space_id: str) -> None:
    del space_id
    _run_reliable_task(self, job_id=job_id, job_kind=JobKind.SOURCE_PARSE)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="knowledge_workbench.source_extract",
    ignore_result=True,
)
def source_extract(self: Task[Any, Any], *, job_id: str, space_id: str) -> None:
    del space_id
    _run_reliable_task(self, job_id=job_id, job_kind=JobKind.SOURCE_EXTRACT)
