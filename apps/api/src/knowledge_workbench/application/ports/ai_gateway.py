from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AIMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class StructuredGenerationRequest(Generic[ResponseT]):
    operation: str
    system_instruction: str
    messages: Sequence[AIMessage]
    response_model: type[ResponseT]
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AIUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class StructuredGenerationResult(Generic[ResponseT]):
    value: ResponseT
    provider: str
    model: str
    usage: AIUsage = AIUsage()
    latency_ms: int = 0
    stop_reason: str = "end_turn"
    provider_request_id: str | None = None


class AIGatewayError(Exception):
    """Base error exposed by the application-owned AI boundary."""


class AIUnavailableError(AIGatewayError):
    pass


class AIRateLimitedError(AIGatewayError):
    pass


class AITimeoutError(AIGatewayError):
    pass


class AIRefusalError(AIGatewayError):
    pass


class AIInvalidOutputError(AIGatewayError):
    pass


class AIGateway(Protocol):
    async def generate_structured(
        self, request: StructuredGenerationRequest[ResponseT]
    ) -> StructuredGenerationResult[ResponseT]: ...


FakeResponder = Callable[
    [StructuredGenerationRequest[Any]],
    BaseModel | Mapping[str, Any] | Awaitable[BaseModel | Mapping[str, Any]],
]
