from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]

from knowledge_workbench.config import get_settings

settings = get_settings()
celery_app = Celery(
    "knowledge_workbench",
    broker=settings.celery_broker_url,
    backend=None,
    include=["knowledge_workbench.worker.tasks"],
)
celery_app.conf.update(
    task_default_queue="source-ingest",
    task_routes={
        "knowledge_workbench.source_ingest": {"queue": "source-ingest"},
        "knowledge_workbench.source_parse": {"queue": "source-parse"},
        "knowledge_workbench.source_extract": {"queue": "source-extract"},
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=False,
    result_backend=None,
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1,
)
