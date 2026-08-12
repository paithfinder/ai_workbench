from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    provider: str
    model: str
    dimensions: int


class EmbeddingError(Exception):
    pass


class EmbeddingUnavailableError(EmbeddingError):
    pass


class EmbeddingInvalidResponseError(EmbeddingError):
    pass


class EmbeddingGateway(Protocol):
    async def embed(self, texts: Sequence[str]) -> EmbeddingResult: ...
