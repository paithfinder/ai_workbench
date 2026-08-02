from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from knowledge_workbench.db.models import OutboxEvent
from knowledge_workbench.worker.outbox_relay import relay_batch


class FakeScalars:
    def __init__(self, events: list[OutboxEvent]) -> None:
        self._events = events

    def __iter__(self):
        return iter(self._events)


class FakeSession:
    def __init__(self, events: list[OutboxEvent]) -> None:
        self.events = events
        self.flushed = False

    async def scalars(self, statement: object) -> FakeScalars:
        del statement
        return FakeScalars(self.events)

    async def flush(self) -> None:
        self.flushed = True


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: list[dict[str, object]] = []
        self.parse_payloads: list[dict[str, object]] = []
        self.cleaned: list[str] = []
        self.fail = fail

    def publish_source_ingest(self, payload):
        if self.fail:
            raise RuntimeError("broker unavailable")
        self.payloads.append(dict(payload))

    def publish_source_parse(self, payload):
        if self.fail:
            raise RuntimeError("broker unavailable")
        self.parse_payloads.append(dict(payload))

    async def cleanup_staging(self, storage_key: str) -> None:
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.cleaned.append(storage_key)


def _event() -> OutboxEvent:
    return OutboxEvent(
        id=uuid4(),
        space_id=uuid4(),
        aggregate_type="job",
        aggregate_id=uuid4(),
        event_type="job.source_ingest.requested",
        deduplication_key="source-ingest:test:1",
        payload={"job_id": str(uuid4()), "space_id": str(uuid4())},
        available_at=datetime.now(UTC),
        attempt_count=0,
        created_at=datetime.now(UTC),
    )


async def test_relay_marks_event_published_after_publish() -> None:
    event = _event()
    publisher = RecordingPublisher()
    session = FakeSession([event])

    count = await relay_batch(session, publisher, batch_size=50)  # type: ignore[arg-type]

    assert count == 1
    assert publisher.payloads == [event.payload]
    assert event.published_at is not None
    assert event.attempt_count == 1
    assert event.last_error is None


async def test_relay_publishes_source_parse_event() -> None:
    event = _event()
    event.event_type = "job.source_parse.requested"
    publisher = RecordingPublisher()
    session = FakeSession([event])

    count = await relay_batch(session, publisher, batch_size=50)  # type: ignore[arg-type]

    assert count == 1
    assert publisher.parse_payloads == [event.payload]
    assert event.published_at is not None


async def test_relay_deletes_staging_object_for_cleanup_event() -> None:
    event = _event()
    event.event_type = "storage.staging_cleanup.requested"
    event.payload = {"storage_key": "uploads/source/version/file.txt"}
    publisher = RecordingPublisher()
    session = FakeSession([event])

    count = await relay_batch(session, publisher, batch_size=50)  # type: ignore[arg-type]

    assert count == 1
    assert publisher.cleaned == ["uploads/source/version/file.txt"]
    assert event.published_at is not None


async def test_relay_defers_event_when_broker_publish_fails() -> None:
    event = _event()
    before = event.available_at
    session = FakeSession([event])

    await relay_batch(  # type: ignore[arg-type]
        session,
        RecordingPublisher(fail=True),
        batch_size=50,
    )

    assert event.published_at is None
    assert event.last_error == "broker unavailable"
    assert event.available_at >= before + timedelta(seconds=2)
