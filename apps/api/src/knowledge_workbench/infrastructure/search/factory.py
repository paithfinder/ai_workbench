from __future__ import annotations

from knowledge_workbench.application.ports.search_provider import (
    SearchProvider,
    SearchRequest,
    SearchResponse,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError


class DisabledSearchProvider(SearchProvider):
    async def search(self, _request: SearchRequest) -> SearchResponse:
        raise AppError(
            "external_search_disabled",
            "External search is not enabled for this environment.",
            status_code=503,
        )


def create_search_provider(settings: Settings) -> SearchProvider:
    if settings.search_provider == "disabled":
        return DisabledSearchProvider()
    if settings.search_provider == "grok":
        from knowledge_workbench.infrastructure.search.grok import GrokSearchProvider

        return GrokSearchProvider(
            endpoint=settings.grok_search_endpoint,
            model=settings.grok_search_model,
            api_key=settings.grok_search_api_key,
            timeout_seconds=settings.search_timeout_seconds,
        )
    raise AssertionError("unknown search provider")
