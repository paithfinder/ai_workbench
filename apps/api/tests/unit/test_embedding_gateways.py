from __future__ import annotations

import math

import httpx
import pytest

from knowledge_workbench.application.ports.embedding_gateway import (
    EmbeddingInvalidResponseError,
    EmbeddingUnavailableError,
)
from knowledge_workbench.infrastructure.ai.bge_m3 import BGEM3HttpEmbeddingGateway
from knowledge_workbench.infrastructure.ai.fake_embedding import FakeEmbeddingGateway


async def test_fake_embedding_is_deterministic_and_normalized() -> None:
    gateway = FakeEmbeddingGateway(model="test", dimensions=16)

    first = await gateway.embed(["alpha beta", ""])
    second = await gateway.embed(["alpha beta", ""])

    assert first == second
    assert len(first.vectors) == 2
    assert len(first.vectors[0]) == 16
    assert math.isclose(sum(value * value for value in first.vectors[0]), 1.0)
    assert first.vectors[1] == [0.0] * 16


def _gateway() -> BGEM3HttpEmbeddingGateway:
    return BGEM3HttpEmbeddingGateway(
        url="https://embedding.test/v1/embeddings",
        model="BAAI/bge-m3",
        dimensions=3,
        timeout_seconds=1,
    )


async def test_bge_adapter_orders_vectors_by_response_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0, 1, 0]},
                    {"index": 0, "embedding": [1, 0, 0]},
                ]
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    result = await _gateway().embed(["first", "second"])

    assert result.vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_bge_adapter_treats_transient_http_status_as_unavailable(
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

    with pytest.raises(EmbeddingUnavailableError):
        await _gateway().embed(["text"])


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"index": 0, "embedding": [1, 2]}]},
        {"data": [{"index": 0, "embedding": [True, 0, 0]}]},
        {"data": [{"index": 0, "embedding": [1, 0, 0]}, {"index": 0, "embedding": [0, 1, 0]}]},
    ],
)
async def test_bge_adapter_rejects_invalid_vectors(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    original = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(EmbeddingInvalidResponseError):
        await _gateway().embed(["text"])


async def test_bge_adapter_rejects_non_finite_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=b'{"data":[{"index":0,"embedding":[NaN,0,0]}]}',
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(EmbeddingInvalidResponseError):
        await _gateway().embed(["text"])
