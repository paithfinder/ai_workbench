from __future__ import annotations

from knowledge_workbench.application.ports.reranker_gateway import RerankerGateway
from knowledge_workbench.config import Settings
from knowledge_workbench.infrastructure.ai.bge_reranker import BGERerankerHttpGateway
from knowledge_workbench.infrastructure.ai.fake_reranker import FakeRerankerGateway


def create_reranker_gateway(settings: Settings) -> RerankerGateway | None:
    if settings.reranker_provider == "disabled":
        return None
    if settings.reranker_provider == "bge_http":
        return BGERerankerHttpGateway(
            url=settings.reranker_url,
            model=settings.reranker_model,
            timeout_seconds=settings.reranker_timeout_seconds,
        )
    return FakeRerankerGateway()
