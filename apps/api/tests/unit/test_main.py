from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from knowledge_workbench.main import sweep_stale_qa_turns


async def test_qa_processing_sweeper_recovers_after_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expire = AsyncMock(side_effect=[RuntimeError("database unavailable"), 1])
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
    monkeypatch.setattr("knowledge_workbench.main.expire_stale_processing_turns", expire)
    monkeypatch.setattr("knowledge_workbench.main.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await sweep_stale_qa_turns(
            object(),  # type: ignore[arg-type]
            timeout_seconds=60,
            interval_seconds=1,
        )

    assert expire.await_count == 2
    assert sleep.await_count == 2


async def test_qa_processing_sweeper_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expire = AsyncMock(side_effect=asyncio.CancelledError)
    sleep = AsyncMock()
    monkeypatch.setattr("knowledge_workbench.main.expire_stale_processing_turns", expire)
    monkeypatch.setattr("knowledge_workbench.main.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await sweep_stale_qa_turns(
            object(),  # type: ignore[arg-type]
            timeout_seconds=60,
            interval_seconds=1,
        )

    expire.assert_awaited_once()
    sleep.assert_not_awaited()
