from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge_workbench.application.ports.ai_gateway import (
    AIGateway,
    AIMessage,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from knowledge_workbench.db.models import KnowledgeUpdateAction

PROMPT_VERSION = "n2.3-v1"
SCHEMA_VERSION = "n2.3-v1"


class ProposalComparisonGenerationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comparison_kind: Literal[
        "support", "supplement", "conflict", "outdated", "duplicate", "no_evidence"
    ]
    suggested_action: KnowledgeUpdateAction | None = None
    summary: str = Field(min_length=1, max_length=20_000)
    suggested_title: str | None = Field(default=None, min_length=1, max_length=500)
    suggested_body: str | None = Field(default=None, min_length=1, max_length=20_000)
    suggested_tags: list[str] = Field(default_factory=list, max_length=20)
    conditions: list[str] = Field(default_factory=list, max_length=12)
    exceptions: list[str] = Field(default_factory=list, max_length=12)
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty_reason: str | None = Field(default=None, min_length=1, max_length=2_000)
    new_evidence_ids: list[str] = Field(default_factory=list, max_length=24)
    existing_evidence_ids: list[str] = Field(default_factory=list, max_length=24)

    @field_validator(
        "suggested_tags",
        "conditions",
        "exceptions",
        "new_evidence_ids",
        "existing_evidence_ids",
    )
    @classmethod
    def reject_blank_or_duplicate_values(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("List values must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("List values must not contain duplicates")
        return cleaned

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> ProposalComparisonGenerationOutput:
        if self.comparison_kind == "no_evidence":
            if (
                self.suggested_action is not None
                or self.suggested_title is not None
                or self.suggested_body is not None
                or self.suggested_tags
                or self.conditions
                or self.exceptions
                or self.confidence is not None
                or self.new_evidence_ids
                or self.existing_evidence_ids
                or self.uncertainty_reason is None
            ):
                raise ValueError(
                    "no_evidence output must contain only summary and uncertainty_reason"
                )
            return self
        if not self.new_evidence_ids:
            raise ValueError("Evidence-backed comparisons require new_evidence_ids")
        if self.suggested_action is None:
            raise ValueError("Evidence-backed comparisons require suggested_action")
        if (
            self.comparison_kind in {"conflict", "outdated", "duplicate"}
            and not self.existing_evidence_ids
        ):
            raise ValueError("This comparison kind requires existing_evidence_ids")
        return self


class ProposalComparisonGenerator:
    async def generate(
        self,
        gateway: AIGateway,
        *,
        candidate_prompt: str,
    ) -> StructuredGenerationResult[ProposalComparisonGenerationOutput]:
        request = StructuredGenerationRequest(
            operation="generate_proposal_comparison",
            system_instruction=(
                "你是严格的知识更新比对器。只能基于给定候选判断新来源与既有知识的关系。"
                "只能在 new_evidence_ids 和 existing_evidence_ids 中引用给定 Candidate ID，"
                "不得编造来源、哈希、定位符、Revision 或其他 ID。"
                "证据不足时必须返回 no_evidence，并提供 uncertainty_reason；"
                "no_evidence 不得附带任何 evidence ID 或建议操作。"
                "support、supplement、conflict、outdated、duplicate 必须至少引用一个新来源候选；"
                "conflict、outdated、duplicate 还必须引用至少一个既有 Evidence 候选。"
                "只提出 draft 建议，绝不批准、应用或修改知识库。"
            ),
            messages=[AIMessage(role="user", content=candidate_prompt)],
            response_model=ProposalComparisonGenerationOutput,
        )
        return await gateway.generate_structured(request)
