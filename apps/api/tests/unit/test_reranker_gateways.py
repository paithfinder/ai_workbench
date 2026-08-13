from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from knowledge_workbench.application.ports.reranker_gateway import (
    RerankDocument,
    RerankerInvalidResponseError,
    RerankerUnavailableError,
)
from knowledge_workbench.infrastructure.ai.bge_reranker import BGERerankerHttpGateway
from knowledge_workbench.infrastructure.ai.fake_reranker import FakeRerankerGateway


def _gateway() -> BGERerankerHttpGateway:
    return BGERerankerHttpGateway(
        url="https://reranker.test/rerank",
        model="BAAI/bge-reranker-v2-m3",
        timeout_seconds=1,
    )


def _documents() -> list[RerankDocument]:
    return [RerankDocument(uuid4(), "first"), RerankDocument(uuid4(), "second")]


async def test_fake_reranker_is_deterministic() -> None:
    documents = _documents()
    gateway = FakeRerankerGateway()

    first = await gateway.rerank(query="question", documents=documents)
    second = await gateway.rerank(query="question", documents=documents)

    assert first == second
    assert [score.id for score in first] == [document.id for document in documents]
    assert all(0 <= score.score <= 1 for score in first)


async def test_bge_reranker_maps_response_indices_to_document_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient
    documents = _documents()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rerank"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    result = await _gateway().rerank(query="question", documents=documents)

    assert [(item.id, item.score) for item in result] == [
        (documents[0].id, 0.2),
        (documents[1].id, 0.8),
    ]


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_bge_reranker_treats_transient_status_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    original = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(RerankerUnavailableError):
        await _gateway().rerank(query="question", documents=_documents())


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"index": 0, "score": 1.0}]},
        {"results": [{"index": 0, "score": 1.0}, {"index": 0, "score": 0.5}]},
        {"results": [{"index": 0, "score": True}, {"index": 1, "score": 0.5}]},
        "non_finite",
    ],
)
async def test_bge_reranker_rejects_invalid_results(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    original = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        if payload == "non_finite":
            return httpx.Response(
                200,
                content=b'{"results":[{"index":0,"score":NaN},{"index":1,"score":0.5}]}',
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(RerankerInvalidResponseError):
        await _gateway().rerank(query="question", documents=_documents())
