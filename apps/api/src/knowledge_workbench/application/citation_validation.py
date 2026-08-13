from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.context_composer import ComposedContext, ContextEvidence
from knowledge_workbench.application.retrieval import ResolvedScope, ScopeResolver
from knowledge_workbench.config import Settings
from knowledge_workbench.db.models import RetrievalChunk


class CitationValidationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    claim_id: str
    claim_text: str
    evidence_ids: list[str]


@dataclass(frozen=True, slots=True)
class ValidatedCitation:
    claim_id: str
    claim_text: str
    evidence_id: str
    chunk_id: UUID
    corpus_kind: str
    content_hash: str
    frozen_quote: str
    locator: dict[str, object] | None
    deep_link: str
    knowledge_node_id: UUID | None
    knowledge_revision_id: UUID | None
    source_id: UUID | None
    source_version_id: UUID | None
    parse_artifact_id: UUID | None
    section_id: UUID | None


class CitationValidator:
    async def validate_context(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        scope: ResolvedScope,
        context: ComposedContext,
    ) -> None:
        if not context.evidence:
            return
        expected = {item.hit.chunk.id for item in context.evidence}
        predicates = ScopeResolver.scope_predicates(
            settings=settings,
            space_id=scope.space_id,
            scope_path=scope.scope_ltree,
            include_descendants=scope.include_descendants,
        )
        current = set(
            await session.scalars(
                select(RetrievalChunk.id).where(RetrievalChunk.id.in_(expected), *predicates)
            )
        )
        if current != expected:
            raise CitationValidationError(
                "Context evidence is no longer valid in the frozen scope"
            )

    def validate_claims(
        self,
        *,
        claims: list[AnswerClaim],
        context: ComposedContext,
    ) -> list[ValidatedCitation]:
        if not claims:
            raise CitationValidationError("An answered response must contain factual claims")
        evidence_by_id = {item.evidence_id: item for item in context.evidence}
        seen_claims: set[str] = set()
        citations: list[ValidatedCitation] = []
        for claim in claims:
            claim_id = claim.claim_id.strip()
            claim_text = claim.claim_text.strip()
            if not claim_id or claim_id in seen_claims or not claim_text:
                raise CitationValidationError(
                    "Claim identities and text must be unique and non-empty"
                )
            seen_claims.add(claim_id)
            evidence_ids = list(dict.fromkeys(claim.evidence_ids))
            if not evidence_ids:
                raise CitationValidationError(f"Claim {claim_id} has no evidence")
            for evidence_id in evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    raise CitationValidationError(
                        f"Claim {claim_id} references unknown evidence {evidence_id}"
                    )
                citations.append(
                    self._citation(
                        claim_id=claim_id,
                        claim_text=claim_text,
                        evidence=evidence,
                    )
                )
        return citations

    @staticmethod
    def _citation(
        *, claim_id: str, claim_text: str, evidence: ContextEvidence
    ) -> ValidatedCitation:
        chunk = evidence.hit.chunk
        if chunk.corpus_kind == "source_evidence":
            if not all(
                (
                    chunk.source_id,
                    chunk.source_version_id,
                    chunk.parse_artifact_id,
                    chunk.section_id,
                )
            ):
                raise CitationValidationError("Source evidence identity is incomplete")
            deep_link = (
                f"/sources/{chunk.source_id}?versionId={chunk.source_version_id}"
                f"&artifactId={chunk.parse_artifact_id}&sectionId={chunk.section_id}"
            )
        elif chunk.corpus_kind == "confirmed_knowledge":
            if chunk.knowledge_node_id is None or chunk.knowledge_revision_id is None:
                raise CitationValidationError("Knowledge evidence identity is incomplete")
            deep_link = f"/knowledge?node={chunk.knowledge_node_id}"
        else:
            raise CitationValidationError("Evidence corpus kind is invalid")
        locator = dict(chunk.locator) if chunk.locator is not None else None
        return ValidatedCitation(
            claim_id=claim_id,
            claim_text=claim_text,
            evidence_id=evidence.evidence_id,
            chunk_id=chunk.id,
            corpus_kind=chunk.corpus_kind,
            content_hash=chunk.content_hash,
            frozen_quote=evidence.text,
            locator=locator,
            deep_link=deep_link,
            knowledge_node_id=chunk.knowledge_node_id,
            knowledge_revision_id=chunk.knowledge_revision_id,
            source_id=chunk.source_id,
            source_version_id=chunk.source_version_id,
            parse_artifact_id=chunk.parse_artifact_id,
            section_id=chunk.section_id,
        )
