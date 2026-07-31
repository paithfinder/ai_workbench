from __future__ import annotations

from pydantic import BaseModel

from knowledge_workbench.application.ports.ai_gateway import (
    AIInvalidOutputError,
    AIMessage,
    StructuredGenerationRequest,
)
from knowledge_workbench.infrastructure.ai.fake import FakeAIGateway


class ExampleOutput(BaseModel):
    title: str


async def test_fake_gateway_returns_validated_fixture() -> None:
    gateway = FakeAIGateway({"example": lambda _: {"title": "固定结果"}})
    request = StructuredGenerationRequest(
        operation="example",
        system_instruction="test",
        messages=[AIMessage(role="user", content="input")],
        response_model=ExampleOutput,
    )

    result = await gateway.generate_structured(request)

    assert result.value == ExampleOutput(title="固定结果")
    assert result.provider == "fake"
    assert gateway.calls == [request]


async def test_fake_gateway_fails_closed_for_unknown_operation() -> None:
    gateway = FakeAIGateway()
    request = StructuredGenerationRequest(
        operation="unknown",
        system_instruction="test",
        messages=[],
        response_model=ExampleOutput,
    )

    try:
        await gateway.generate_structured(request)
    except AIInvalidOutputError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("Unknown fake operation must fail closed")
