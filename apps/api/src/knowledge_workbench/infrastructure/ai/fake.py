from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from knowledge_workbench.application.ports.ai_gateway import (
    AIInvalidOutputError,
    FakeResponder,
    ResponseT,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)


class FakeAIGateway:
    """Deterministic D1 adapter. It never reads credentials or opens a network client."""

    def __init__(self, responders: Mapping[str, FakeResponder] | None = None) -> None:
        self._responders = dict(responders or {})
        self.calls: list[StructuredGenerationRequest[Any]] = []

    async def generate_structured(
        self, request: StructuredGenerationRequest[ResponseT]
    ) -> StructuredGenerationResult[ResponseT]:
        self.calls.append(request)
        responder = self._responders.get(request.operation)
        if responder is None:
            raise AIInvalidOutputError(
                f"No deterministic fake response registered for operation {request.operation!r}"
            )

        raw = responder(request)
        if inspect.isawaitable(raw):
            raw = await raw

        try:
            if isinstance(raw, request.response_model):
                value = raw
            elif isinstance(raw, BaseModel):
                value = request.response_model.model_validate(raw.model_dump())
            else:
                value = request.response_model.model_validate(raw)
        except ValidationError as exc:
            raise AIInvalidOutputError("Fake response did not match the response schema") from exc

        return StructuredGenerationResult(
            value=value,
            provider="fake",
            model="fake-deterministic-v1",
            provider_request_id=f"fake:{request.operation}:{len(self.calls)}",
        )
