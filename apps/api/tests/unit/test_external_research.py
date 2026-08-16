from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from knowledge_workbench.application.external_research import (
    ExternalResearchService,
    SearchResearchRequest,
    SourceSelectionInput,
    SourceSelectionRequest,
    _normalize_selections,
)
from knowledge_workbench.application.ports.search_provider import SearchRequest
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import ResearchRun, ResearchRunStatus
from knowledge_workbench.infrastructure.search.grok import GrokSearchProvider


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SearchSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.result: object | None = None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def scalar(self, _statement: object) -> None:
        return None

    async def execute(self, _statement: object) -> object:
        return self.result

    def add_all(self, items: list[object]) -> None:
        self.added.extend(items)

    async def flush(self) -> None:
        return None


def test_external_search_defaults_are_disabled_and_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.search_provider == "disabled"
    assert settings.search_max_results == 10
    assert settings.research_selection_max_urls == 10
    assert settings.grok_search_api_key is None


async def test_unconfigured_service_marks_created_research_run_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ExternalResearchService()
    session = _SearchSession()
    mark_failed = AsyncMock()
    monkeypatch.setattr(service, "_space", AsyncMock())
    monkeypatch.setattr(service, "_mark_search_failed", mark_failed)

    with pytest.raises(AppError) as caught:
        await service.search(
            session,  # type: ignore[arg-type]
            space_id=uuid4(),
            idempotency_key="search-without-provider",
            request=SearchResearchRequest(query="Example"),
            max_results=10,
        )

    research_run = next(item for item in session.added if isinstance(item, ResearchRun))
    assert caught.value.code == "external_search_not_configured"
    mark_failed.assert_awaited_once_with(
        session,
        space_id=research_run.space_id,
        research_run_id=research_run.id,
        error=caught.value,
    )


async def test_existing_web_record_reuses_matching_source_version_and_job() -> None:
    service = ExternalResearchService()
    session = _SearchSession()
    source_id, version_id, job_id = uuid4(), uuid4(), uuid4()
    row = (source_id, version_id, job_id, "sources/example.html")
    session.result = MagicMock(one_or_none=MagicMock(return_value=row))

    record = await service._existing_web_record(  # noqa: SLF001
        session,  # type: ignore[arg-type]
        space_id=uuid4(),
        requested_url="https://example.com/article",
    )

    assert record is not None
    assert (record.source_id, record.version_id, record.job_id) == (
        source_id,
        version_id,
        job_id,
    )
    assert record.storage_key == "sources/example.html"


async def test_mark_search_failed_records_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ExternalResearchService()
    session = _SearchSession()
    research_run = ResearchRun(
        id=uuid4(),
        space_id=uuid4(),
        origin="external_search",
        status=ResearchRunStatus.DRAFT.value,
        query="Example",
        search_filters={},
        started_at=datetime.now(UTC),
    )
    monkeypatch.setattr(service, "_run", AsyncMock(return_value=research_run))
    error = AppError(
        "external_search_not_configured",
        "External search is not configured for this environment.",
        status_code=503,
    )

    await service._mark_search_failed(  # noqa: SLF001
        session,  # type: ignore[arg-type]
        space_id=research_run.space_id,
        research_run_id=research_run.id,
        error=error,
    )

    assert research_run.status == ResearchRunStatus.FAILED.value
    assert research_run.failure_code == error.code
    assert research_run.failure_message == error.message
    assert research_run.completed_at is not None


async def test_grok_provider_returns_validated_structured_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "grok-test"
        assert payload["tools"] == [{"type": "web_search"}]
        return httpx.Response(
            200,
            headers={"x-request-id": "request-1"},
            json={
                "output_text": json.dumps(
                    [
                        {
                            "title": "Example",
                            "url": "https://example.com/article",
                            "snippet": "A result.",
                        }
                    ]
                )
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    provider = GrokSearchProvider(
        endpoint="https://search.test/responses",
        model="grok-test",
        api_key=SecretStr("test-secret"),
        timeout_seconds=1,
    )

    response = await provider.search(SearchRequest(query="example", max_results=3, filters={}))

    assert response.provider == "grok"
    assert response.model == "grok-test"
    assert response.request_id == "request-1"
    assert response.results[0].url == "https://example.com/article"


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_grok_provider_maps_transient_responses_without_secret_leakage(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    original = httpx.AsyncClient

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="test-secret")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    provider = GrokSearchProvider(
        endpoint="https://search.test/responses",
        model="grok-test",
        api_key=SecretStr("test-secret"),
        timeout_seconds=1,
    )

    with pytest.raises(AppError) as caught:
        await provider.search(SearchRequest(query="example", max_results=3, filters={}))

    assert caught.value.code in {"external_search_rate_limited", "external_search_unavailable"}
    assert "test-secret" not in caught.value.message


def test_search_request_and_selection_dtos_reject_unknown_fields() -> None:
    with pytest.raises(ValueError):
        SearchResearchRequest(query="example", unexpected=True)
    with pytest.raises(ValueError):
        SourceSelectionRequest(
            selections=[{"url": "https://example.com", "title": "Example", "extra": "no"}]
        )


def test_source_selection_normalizes_and_deduplicates_urls() -> None:
    selections = _normalize_selections(
        [
            SourceSelectionInput(url="https://example.com#first", title="First"),
            SourceSelectionInput(url="https://example.com", title="Duplicate"),
            SourceSelectionInput(url="https://example.org/path", title="Second"),
        ],
        max_urls=2,
    )

    assert [(item.title, item.url) for item in selections] == [
        ("First", "https://example.com"),
        ("Second", "https://example.org/path"),
    ]


def test_source_selection_limit_is_enforced_after_normalization() -> None:
    with pytest.raises(AppError) as caught:
        _normalize_selections(
            [
                SourceSelectionInput(url="https://example.com", title="One"),
                SourceSelectionInput(url="https://example.org", title="Two"),
            ],
            max_urls=1,
        )

    assert caught.value.code == "source_selection_limit_exceeded"
