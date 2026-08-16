from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    max_results: int
    filters: dict[str, str]


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str | None = None
    metadata: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class SearchResponse:
    provider: str
    model: str | None
    request_id: str | None
    results: tuple[SearchResult, ...]


class SearchProvider(Protocol):
    async def search(self, request: SearchRequest) -> SearchResponse: ...
