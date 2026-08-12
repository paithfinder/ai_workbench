from __future__ import annotations

import hashlib
from collections.abc import Sequence

from knowledge_workbench.application.ports.embedding_gateway import EmbeddingResult


class FakeEmbeddingGateway:
    def __init__(self, *, model: str, dimensions: int) -> None:
        self._model = model
        self._dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._dimensions
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self._dimensions
                vector[index] += 1.0 if digest[4] % 2 else -1.0
            norm = sum(value * value for value in vector) ** 0.5
            if norm:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return EmbeddingResult(
            vectors=vectors,
            provider="fake",
            model=self._model,
            dimensions=self._dimensions,
        )
