from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from knowledge_workbench.api.qa import (
    QaTurnRequest,
    _expire_stale_processing_turn,
    create_turn,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import QaTurn


async def test_create_qa_turn_rejects_blank_question_before_database_access() -> None:
    with pytest.raises(AppError) as error:
        await create_turn(
            space_id=uuid4(),
            body=QaTurnRequest(question="   "),
            idempotency_key="qa-test",
            session=object(),  # type: ignore[arg-type]
            settings=Settings(app_env="test"),  # type: ignore[arg-type]
        )

    assert error.value.code == "invalid_qa_question"
    assert error.value.status_code == 422


async def test_create_qa_turn_rejects_blank_idempotency_key_before_database_access() -> None:
    with pytest.raises(AppError) as error:
        await create_turn(
            space_id=uuid4(),
            body=QaTurnRequest(question="问题"),
            idempotency_key="   ",
            session=object(),  # type: ignore[arg-type]
            settings=Settings(app_env="test"),  # type: ignore[arg-type]
        )

    assert error.value.code == "invalid_idempotency_key"
    assert error.value.status_code == 422


async def test_stale_processing_turn_is_failed_durably() -> None:
    turn = QaTurn()
    turn.id = uuid4()
    turn.status = "processing"
    turn.started_at = datetime.now(UTC) - timedelta(seconds=61)
    turn.claims = []
    session = AsyncMock()
    session.scalar.return_value = turn

    result = await _expire_stale_processing_turn(
        session,  # type: ignore[arg-type]
        turn,
        timeout_seconds=60,
    )

    assert result is turn
    assert turn.status == "failed"
    assert turn.error_code == "qa_processing_timeout"
    assert turn.completed_at is not None
    session.commit.assert_awaited_once()


async def test_recent_processing_turn_is_left_unchanged() -> None:
    turn = QaTurn()
    turn.status = "processing"
    turn.started_at = datetime.now(UTC)
    session = AsyncMock()

    result = await _expire_stale_processing_turn(
        session,  # type: ignore[arg-type]
        turn,
        timeout_seconds=60,
    )

    assert result is turn
    assert turn.status == "processing"
    session.scalar.assert_not_awaited()
    session.commit.assert_not_awaited()


def test_qa_routes_document_standard_500_envelope() -> None:
    from knowledge_workbench.config import Settings
    from knowledge_workbench.main import create_app

    responses = create_app(Settings(app_env="test")).openapi()["paths"]
    qa_paths = [
        "/api/v1/knowledge-spaces/{space_id}/qa/turns",
        "/api/v1/knowledge-spaces/{space_id}/qa/turns/{turn_id}",
        "/api/v1/knowledge-spaces/{space_id}/qa/turns/by-idempotency-key/{idempotency_key}",
    ]
    for path in qa_paths:
        operation = responses[path]["post" if path.endswith("/turns") else "get"]
        assert operation["responses"]["500"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorEnvelope"
        }
