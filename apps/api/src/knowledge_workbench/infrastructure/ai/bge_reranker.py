from __future__ import annotations

import math
from collections.abc import Sequence

import httpx

from knowledge_workbench.application.ports.reranker_gateway import (
    RerankDocument,
    RerankerInvalidResponseError,
    RerankerUnavailableError,
    RerankScore,
)


class BGERerankerHttpGateway:
    def __init__(self, *, url: str, model: str, timeout_seconds: float) -> None:
        self._url = url
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def rerank(
        self, *, query: str, documents: Sequence[RerankDocument]
    ) -> list[RerankScore]:
        if not documents:
            return []
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    self._url,
                    json={
                        "model": self._model,
                        "query": query,
                        "documents": [document.text for document in documents],
                        "top_n": len(documents),
                    },
                )
        except httpx.TimeoutException as exc:
            raise RerankerUnavailableError("Reranker request timed out") from exc
        except httpx.RequestError as exc:
            raise RerankerUnavailableError("Reranker service is unavailable") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise RerankerUnavailableError(
                f"Reranker service returned {response.status_code}"
            )
        if response.status_code >= 300:
            raise RerankerInvalidResponseError(
                f"Reranker request was rejected with {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RerankerInvalidResponseError("Reranker response is not valid JSON") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise RerankerInvalidResponseError("Reranker response has no results array")
        by_index: dict[int, float] = {}
        for item in results:
            if not isinstance(item, dict):
                raise RerankerInvalidResponseError("Reranker response item is invalid")
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(documents)
                or index in by_index
                or not isinstance(score, int | float)
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise RerankerInvalidResponseError("Reranker response item is invalid")
            by_index[index] = float(score)
        if len(by_index) != len(documents):
            raise RerankerInvalidResponseError("Reranker response size does not match input")
        return [
            RerankScore(document.id, by_index[index])
            for index, document in enumerate(documents)
        ]
