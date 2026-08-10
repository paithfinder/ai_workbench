from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from pydantic import BaseModel

from knowledge_workbench.application.ports.ai_gateway import (
    AIInvalidOutputError,
    AIMessage,
    AIRefusalError,
    StructuredGenerationRequest,
)
from knowledge_workbench.infrastructure.ai.claude import ClaudeAIGateway


class _Output(BaseModel):
    title: str


class _Messages:
    def __init__(self, response: object) -> None:
        self.response = response
        self.kwargs: dict[str, Any] | None = None

    async def parse(self, **kwargs: Any) -> object:
        self.kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _Client:
    def __init__(self, response: object) -> None:
        self.messages = _Messages(response)


def _request() -> StructuredGenerationRequest[_Output]:
    return StructuredGenerationRequest(
        operation="test",
        system_instruction="system",
        messages=[AIMessage(role="user", content="input")],
        response_model=_Output,
    )


def _response(*, parsed_output: _Output | None, stop_reason: str = "end_turn") -> object:
    return SimpleNamespace(
        parsed_output=parsed_output,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=12, output_tokens=7),
        model="claude-opus-5",
        _request_id="request-1",
    )


async def test_claude_gateway_uses_structured_output_and_tracks_usage() -> None:
    client = _Client(_response(parsed_output=_Output(title="结果")))
    gateway = ClaudeAIGateway(
        model="claude-opus-5",
        timeout_seconds=10,
        max_tokens=1024,
        client=client,  # type: ignore[arg-type]
    )

    result = await gateway.generate_structured(_request())

    assert result.value.title == "结果"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 7
    assert result.provider_request_id == "request-1"
    assert client.messages.kwargs is not None
    assert client.messages.kwargs["output_format"] is _Output
    assert client.messages.kwargs["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (_response(parsed_output=None), AIInvalidOutputError),
        (_response(parsed_output=None, stop_reason="max_tokens"), AIInvalidOutputError),
        (_response(parsed_output=None, stop_reason="refusal"), AIRefusalError),
    ],
)
async def test_claude_gateway_fails_closed_for_unsafe_outputs(
    response: object, error_type: type[Exception]
) -> None:
    gateway = ClaudeAIGateway(
        model="claude-opus-5",
        timeout_seconds=10,
        max_tokens=1024,
        client=_Client(response),  # type: ignore[arg-type]
    )

    with pytest.raises(error_type):
        await gateway.generate_structured(_request())


def test_anthropic_sdk_dependency_is_official() -> None:
    assert anthropic.AsyncAnthropic.__module__.startswith("anthropic")
