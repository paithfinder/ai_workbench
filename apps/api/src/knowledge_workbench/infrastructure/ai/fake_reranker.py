from __future__ import annotations

import hashlib
from collections.abc import Sequence

from knowledge_workbench.application.ports.reranker_gateway import RerankDocument, RerankScore


class FakeRerankerGateway:
    async def rerank(
        self, *, query: str, documents: Sequence[RerankDocument]
    ) -> list[RerankScore]:
        return [
            RerankScore(
                document.id,
                int.from_bytes(
                    hashlib.sha256(f"{query}\0{document.text}".encode()).digest()[:8], "big"
                )
                / (2**64 - 1),
            )
            for document in documents
        ]
