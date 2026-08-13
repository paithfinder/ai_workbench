from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowledge_workbench.application.citation_validation import AnswerClaim
from knowledge_workbench.application.context_composer import ComposedContext
from knowledge_workbench.application.ports.ai_gateway import (
    AIGateway,
    AIMessage,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)


class QaClaimOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=100)
    claim_text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class QaGenerationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["answered", "abstained"]
    answer: str | None = Field(default=None, max_length=20_000)
    abstain_code: Literal["insufficient_evidence"] | None = None
    claims: list[QaClaimOutput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_result_shape(self) -> QaGenerationOutput:
        if self.status == "answered":
            if self.answer is None or not self.answer.strip() or not self.claims:
                raise ValueError("answered output requires answer and claims")
            if self.abstain_code is not None:
                raise ValueError("answered output cannot include abstain_code")
        elif self.answer is not None or self.claims or self.abstain_code is None:
            raise ValueError("abstained output requires only abstain_code")
        return self

    def answer_claims(self) -> list[AnswerClaim]:
        return [
            AnswerClaim(claim.claim_id, claim.claim_text, claim.evidence_ids)
            for claim in self.claims
        ]

    def rendered_answer(self) -> str:
        return "\n".join(claim.claim_text.strip() for claim in self.claims)


class QaGenerator:
    async def generate(
        self,
        gateway: AIGateway,
        *,
        question: str,
        context: ComposedContext,
    ) -> StructuredGenerationResult[QaGenerationOutput]:
        if not context.evidence:
            raise ValueError("Cannot generate an answer without evidence")
        request = StructuredGenerationRequest(
            operation="answer_qa_turn",
            system_instruction=(
                "你是严格的知识库问答器。只能依据给定证据回答，不得补充常识。"
                "每个事实 claim 必须引用一个或多个给定的 Evidence ID。"
                "answer 仅用于兼容结构，服务端将按 claims 顺序确定性发布 claim_text。"
                "因此必须把答案中的每个事实完整拆入 claims，不得只在 answer 中补充。"
                "证据不足时返回 abstained 和 insufficient_evidence。"
            ),
            messages=[
                AIMessage(
                    role="user",
                    content=f"问题：\n{question}\n\n证据：\n{context.prompt_text()}",
                )
            ],
            response_model=QaGenerationOutput,
            metadata={"evidence_count": str(len(context.evidence))},
        )
        return await gateway.generate_structured(request)
