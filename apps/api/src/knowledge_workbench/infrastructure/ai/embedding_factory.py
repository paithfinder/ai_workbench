from __future__ import annotations

from knowledge_workbench.application.ports.embedding_gateway import EmbeddingGateway
from knowledge_workbench.config import Settings
from knowledge_workbench.infrastructure.ai.bge_m3 import BGEM3HttpEmbeddingGateway
from knowledge_workbench.infrastructure.ai.fake_embedding import FakeEmbeddingGateway


def create_embedding_gateway(settings: Settings) -> EmbeddingGateway:
    if settings.embedding_provider == "bge_m3_http":
        return BGEM3HttpEmbeddingGateway(
            url=settings.embedding_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    return FakeEmbeddingGateway(
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
