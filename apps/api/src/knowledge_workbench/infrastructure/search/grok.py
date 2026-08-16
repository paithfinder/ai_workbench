from __future__ import annotations

import json

import httpx
from pydantic import SecretStr

from knowledge_workbench.application.ports.search_provider import (
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from knowledge_workbench.core.errors import AppError


class GrokSearchProvider:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: SecretStr | None,
        timeout_seconds: float,
    ) -> None:
        if api_key is None or not api_key.get_secret_value():
            raise AppError(
                "external_search_not_configured",
                "External search is not configured for this environment.",
                status_code=503,
            )
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def search(self, request: SearchRequest) -> SearchResponse:
        payload = {
            "model": self._model,
            "input": _prompt(request),
            "tools": [{"type": "web_search"}],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds, trust_env=False) as client:
                response = await client.post(
                    self._endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise AppError(
                "external_search_timeout",
                "External search did not respond in time. Please retry.",
                status_code=503,
            ) from exc
        except httpx.HTTPError as exc:
            raise AppError(
                "external_search_unavailable",
                "External search is temporarily unavailable. Please retry.",
                status_code=503,
            ) from exc

        if response.status_code == 429:
            raise AppError(
                "external_search_rate_limited",
                "External search is rate limited. Please retry later.",
                status_code=503,
            )
        if response.status_code >= 500:
            raise AppError(
                "external_search_unavailable",
                "External search is temporarily unavailable. Please retry.",
                status_code=503,
            )
        if not response.is_success:
            raise AppError(
                "external_search_failed",
                "External search rejected the request.",
                status_code=422,
            )
        try:
            body = response.json()
            results = _results_from_response(body)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AppError(
                "external_search_invalid_response",
                "External search returned an invalid result.",
                status_code=503,
            ) from exc
        return SearchResponse(
            provider="grok",
            model=self._model,
            request_id=_optional_string(response.headers.get("x-request-id"), maximum=255),
            results=tuple(results[: request.max_results]),
        )


def _prompt(request: SearchRequest) -> str:
    filters = "\n".join(f"{key}: {value}" for key, value in sorted(request.filters.items()))
    return (
        "Search the web for the query below. Return JSON only: an array of objects with "
        "title, url, and optional snippet fields. Do not include commentary.\n"
        f"Query: {request.query}\n"
        f"Maximum results: {request.max_results}\n"
        f"Filters:\n{filters or 'none'}"
    )


def _results_from_response(body: object) -> list[SearchResult]:
    if not isinstance(body, dict):
        raise ValueError("response must be an object")
    raw = body.get("output_text")
    if not isinstance(raw, str):
        raw = _extract_output_text(body.get("output"))
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("output must be a result array")
    results: list[SearchResult] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("result must be an object")
        title = _required_string(item.get("title"), maximum=500)
        url = _required_string(item.get("url"), maximum=2000)
        snippet = _optional_string(item.get("snippet"), maximum=10_000)
        results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


def _extract_output_text(output: object) -> str:
    if not isinstance(output, list):
        raise ValueError("response has no output text")
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
    if len(texts) != 1:
        raise ValueError("response has ambiguous output text")
    return texts[0]


def _required_string(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("result field must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError("result field is invalid")
    return cleaned


def _optional_string(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_string(value, maximum=maximum)
