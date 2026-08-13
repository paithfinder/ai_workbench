from __future__ import annotations

from dataclasses import dataclass

from knowledge_workbench.application.hybrid_retrieval import HybridRetrievalHit


@dataclass(frozen=True, slots=True)
class ContextEvidence:
    evidence_id: str
    ordinal: int
    hit: HybridRetrievalHit
    text: str


@dataclass(frozen=True, slots=True)
class ComposedContext:
    evidence: list[ContextEvidence]
    total_characters: int

    def prompt_text(self) -> str:
        blocks: list[str] = []
        for item in self.evidence:
            title = item.hit.chunk.title or "未命名证据"
            blocks.append(f"[{item.evidence_id}] {title}\n{item.text}")
        return "\n\n".join(blocks)


class ContextComposer:
    def compose(
        self,
        hits: list[HybridRetrievalHit],
        *,
        max_chunks: int,
        max_characters: int,
        max_chunk_characters: int,
    ) -> ComposedContext:
        if min(max_chunks, max_characters, max_chunk_characters) <= 0:
            raise ValueError("Context limits must be positive")
        evidence: list[ContextEvidence] = []
        seen: set[object] = set()
        total = 0
        ordered = sorted(hits, key=lambda hit: (hit.final_rank, hit.chunk.id))
        for hit in ordered:
            if hit.chunk.id in seen or len(evidence) >= max_chunks:
                continue
            seen.add(hit.chunk.id)
            remaining = max_characters - total
            if remaining <= 0:
                break
            text = hit.chunk.text[: min(max_chunk_characters, remaining)].strip()
            if not text:
                continue
            ordinal = len(evidence)
            evidence.append(
                ContextEvidence(
                    evidence_id=f"E{ordinal + 1}",
                    ordinal=ordinal,
                    hit=hit,
                    text=text,
                )
            )
            total += len(text)
        return ComposedContext(evidence=evidence, total_characters=total)
