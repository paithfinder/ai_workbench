from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from knowledge_workbench.api import retrieval as retrieval_api
from knowledge_workbench.api.retrieval import DebugRetrievalRequest, ScopePreviewRequest
from knowledge_workbench.application.ports.embedding_gateway import EmbeddingUnavailableError
from knowledge_workbench.application.retrieval import ResolvedScope
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError


def _scope() -> ResolvedScope:
    node_id = uuid4()
    return ResolvedScope(
        space_id=uuid4(),
        scope_node_id=node_id,
        scope_ltree=f"n{node_id.hex}",
        scope_path="整个知识库 / 测试范围",
        node_kind="folder",
        node_title="测试范围",
        include_descendants=True,
        knowledge_count=1,
        source_count=0,
        source_version_count=0,
        chunk_count=1,
        index_status="ready",
        index_config_version="d7-v1",
        scope_snapshot_hash="a" * 64,
    )


def test_retrieval_debug_gate_fails_closed_outside_development_and_test() -> None:
    with pytest.raises(AppError) as error:
        retrieval_api._require_debug(Settings(app_env="production"))  # type: ignore[arg-type]

    assert error.value.code == "retrieval_debug_disabled"
    assert error.value.status_code == 404


async def test_debug_search_rejects_whitespace_only_query_before_database_access() -> None:
    with pytest.raises(AppError) as error:
        await retrieval_api.debug_search(
            space_id=uuid4(),
            body=DebugRetrievalRequest(
                query="   ",
                scope=ScopePreviewRequest(),
            ),
            session=object(),  # type: ignore[arg-type]
            settings=Settings(app_env="test"),  # type: ignore[arg-type]
        )

    assert error.value.code == "invalid_retrieval_query"
    assert error.value.status_code == 422


async def test_vector_failure_preserves_keyword_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()

    async def resolve_scope(*args: Any, **kwargs: Any) -> ResolvedScope:
        del args, kwargs
        return scope

    async def keyword(*args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        return []

    async def vector(*args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        raise EmbeddingUnavailableError("embedding gateway unavailable")

    monkeypatch.setattr(retrieval_api.ScopeResolver, "resolve", resolve_scope)
    monkeypatch.setattr(retrieval_api.DebugRetrievalService, "keyword", keyword)
    monkeypatch.setattr(retrieval_api.DebugRetrievalService, "vector", vector)

    response = await retrieval_api.debug_search(
        space_id=scope.space_id,
        body=DebugRetrievalRequest(
            query="测试问题",
            scope=ScopePreviewRequest(scope_node_id=scope.scope_node_id),
        ),
        session=object(),  # type: ignore[arg-type]
        settings=Settings(app_env="test"),  # type: ignore[arg-type]
    )

    assert response.keyword_hits == []
    assert response.vector_hits == []
    assert response.channel_errors == {
        "keyword": None,
        "vector": "embedding gateway unavailable",
    }
    assert response.embedding is None
    assert response.scope_summary.scope_snapshot_hash == scope.scope_snapshot_hash
