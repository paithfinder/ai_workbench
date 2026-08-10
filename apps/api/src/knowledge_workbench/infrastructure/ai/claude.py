from __future__ import annotations

import time
from typing import Any

import anthropic
from pydantic import ValidationError

from knowledge_workbench.application.ports.ai_gateway import (
    AIGatewayError,
    AIInvalidOutputError,
    AIRateLimitedError,
    AIRefusalError,
    AITimeoutError,
    AIUnavailableError,
    AIUsage,
    ResponseT,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)


class ClaudeAIGateway:
    """Official Anthropic SDK adapter for application-owned structured generation."""

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = client or anthropic.AsyncAnthropic(
            timeout=timeout_seconds,
            max_retries=2,
        )

    async def generate_structured(
        self, request: StructuredGenerationRequest[ResponseT]
    ) -> StructuredGenerationResult[ResponseT]:
        started = time.perf_counter()
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                system=request.system_instruction,
                messages=[
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                output_format=request.response_model,
                thinking={"type": "adaptive"},
                output_config={"effort": "low"},
            )
        except anthropic.APITimeoutError as exc:
            raise AITimeoutError("Claude generation timed out") from exc
        except anthropic.RateLimitError as exc:
            raise AIRateLimitedError("Claude rate limit was reached") from exc
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise AIUnavailableError("Claude is temporarily unavailable") from exc
        except (anthropic.BadRequestError, anthropic.AuthenticationError) as exc:
            raise AIGatewayError("Claude request configuration was rejected") from exc
        except anthropic.APIStatusError as exc:
            raise AIGatewayError(f"Claude request failed with HTTP {exc.status_code}") from exc
        except ValidationError as exc:
            raise AIInvalidOutputError("Claude output did not match the response schema") from exc

        if response.stop_reason == "refusal":
            raise AIRefusalError("Claude declined the extraction request")
        if response.stop_reason == "max_tokens":
            raise AIInvalidOutputError("Claude output exceeded the configured token limit")
        value = response.parsed_output
        if value is None:
            raise AIInvalidOutputError("Claude returned no structured output")

        usage: Any = response.usage
        return StructuredGenerationResult(
            value=value,
            provider="anthropic",
            model=response.model,
            usage=AIUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
            latency_ms=round((time.perf_counter() - started) * 1000),
            stop_reason=response.stop_reason or "end_turn",
            provider_request_id=response._request_id,
        )
