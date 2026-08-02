from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WebFetchResult:
    requested_url: str
    final_url: str
    fetched_at: datetime
    body: bytes
    content_sha256: str
    media_type: str
    headers: dict[str, str]
    status_code: int


class WebFetcher(Protocol):
    async def fetch(self, url: str) -> WebFetchResult: ...
