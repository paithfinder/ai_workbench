from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from knowledge_workbench.config import Settings
from knowledge_workbench.worker.source_ingest import (
    ActiveJobLeaseError,
    ClaimToken,
    PermanentIngestError,
    SourceIngestWorker,
)


class FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class FailingWorker(SourceIngestWorker):
    def __init__(self, error: Exception) -> None:
        super().__init__(Settings(app_env="test"))
        self.error = error
        self.token = ClaimToken(uuid4(), 1)
        self.failure: tuple[str, bool] | None = None

    async def _claim(self, *args, **kwargs):
        del args, kwargs
        if isinstance(self.error, OperationalError):
            raise self.error
        return self.token

    async def _heartbeat(self, *args, **kwargs):
        del args, kwargs
        return True

    async def _complete(self, *args, **kwargs):
        del args, kwargs
        raise self.error

    async def _mark_failed(self, *args, code: str, retryable: bool, **kwargs):
        del args, kwargs
        self.failure = (code, retryable)
        return True


async def test_claim_transient_database_error_is_re_raised_for_celery_retry() -> None:
    worker = FailingWorker(OperationalError("claim", {}, RuntimeError("database down")))
    session = FakeSession()

    with pytest.raises(OperationalError):
        await worker.run(  # type: ignore[arg-type]
            session,
            job_id=uuid4(),
            celery_task_id="task-1",
            worker_name="worker-1",
        )

    assert session.rollbacks == 1
    assert worker.failure is None


async def test_permanent_ingest_error_is_not_marked_retryable() -> None:
    worker = FailingWorker(PermanentIngestError("source_not_found", "Source missing"))
    session = FakeSession()

    await worker.run(  # type: ignore[arg-type]
        session,
        job_id=uuid4(),
        celery_task_id="task-1",
        worker_name="worker-1",
    )

    assert worker.failure == ("source_not_found", False)


def test_active_job_lease_has_positive_retry_delay() -> None:
    assert ActiveJobLeaseError(0).retry_after_seconds == 1
    assert ActiveJobLeaseError(37).retry_after_seconds == 37
