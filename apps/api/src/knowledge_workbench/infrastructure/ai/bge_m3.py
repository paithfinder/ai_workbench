from __future__ import annotations

import math
from collections.abc import Sequence

import httpx

from knowledge_workbench.application.ports.embedding_gateway import (
    EmbeddingInvalidResponseError,
    EmbeddingResult,
    EmbeddingUnavailableError,
)


class BGEM3HttpEmbeddingGateway:
    def __init__(self, *, url: str, model: str, dimensions: int, timeout_seconds: float) -> None:
        self._url = url
        self._model = model
        self._dimensions = dimensions
        self._timeout_seconds = timeout_seconds

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult([], "bge_m3_http", self._model, self._dimensions)
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    self._url, json={"model": self._model, "input": list(texts)}
                )
        except httpx.TimeoutException as exc:
            raise EmbeddingUnavailableError("BGE-M3 embedding request timed out") from exc
        except httpx.RequestError as exc:
            raise EmbeddingUnavailableError("BGE-M3 embedding service is unavailable") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise EmbeddingUnavailableError(
                f"BGE-M3 embedding service returned {response.status_code}"
            )
        if 300 <= response.status_code < 500:
            raise EmbeddingInvalidResponseError(
                f"BGE-M3 embedding request was rejected with {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingInvalidResponseError("Embedding response is not valid JSON") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise EmbeddingInvalidResponseError("Embedding response has no data array")
        ordered: list[tuple[int, list[float]]] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                raise EmbeddingInvalidResponseError("Embedding response item is invalid")
            index = item.get("index", position)
            embedding = item.get("embedding")
            if not isinstance(index, int) or not 0 <= index < len(texts):
                raise EmbeddingInvalidResponseError("Embedding response index is invalid")
            if (
                not isinstance(embedding, list)
                or len(embedding) != self._dimensions
                or not all(
                    isinstance(value, int | float)
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    for value in embedding
                )
            ):
                raise EmbeddingInvalidResponseError("Embedding response has an invalid vector")
            ordered.append((index, [float(value) for value in embedding]))
        if len(ordered) != len(texts) or len({index for index, _ in ordered}) != len(texts):
            raise EmbeddingInvalidResponseError("Embedding response size does not match input")
        ordered.sort(key=lambda item: item[0])
        return EmbeddingResult(
            [vector for _, vector in ordered],
            "bge_m3_http",
            self._model,
            self._dimensions,
        )
