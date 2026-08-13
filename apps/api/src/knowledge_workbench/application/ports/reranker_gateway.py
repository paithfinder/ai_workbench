from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RerankDocument:
    id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class RerankScore:
    id: UUID
    score: float


class RerankerError(Exception):
    """Base error exposed by the application-owned reranker boundary."""


class RerankerUnavailableError(RerankerError):
    pass


class RerankerInvalidResponseError(RerankerError):
    pass


class RerankerGateway(Protocol):
    async def rerank(
        self, *, query: str, documents: Sequence[RerankDocument]
    ) -> list[RerankScore]: ...
