from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowledge_workbench.application.context_composer import ComposedContext
from knowledge_workbench.application.qa_generation import (
    QaGenerationOutput,
    QaGenerator,
)
from knowledge_workbench.infrastructure.ai.fake import FakeAIGateway


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "answered", "answer": "", "claims": []},
        {
            "status": "answered",
            "answer": "回答",
            "abstain_code": "insufficient_evidence",
            "claims": [{"claim_id": "C1", "claim_text": "事实", "evidence_ids": ["E1"]}],
        },
        {"status": "abstained", "answer": "不应存在", "abstain_code": "insufficient_evidence"},
    ],
)
def test_qa_output_rejects_inconsistent_result_shapes(payload: object) -> None:
    with pytest.raises(ValidationError):
        QaGenerationOutput.model_validate(payload)


def test_rendered_answer_is_determined_only_by_claims() -> None:
    output = QaGenerationOutput.model_validate(
        {
            "status": "answered",
            "answer": "模型额外写入的未引用内容",
            "claims": [
                {"claim_id": "C1", "claim_text": "事实一", "evidence_ids": ["E1"]},
                {"claim_id": "C2", "claim_text": "事实二", "evidence_ids": ["E2"]},
            ],
        }
    )

    assert output.rendered_answer() == "事实一\n事实二"


async def test_qa_generator_does_not_call_model_without_evidence() -> None:
    gateway = FakeAIGateway()

    with pytest.raises(ValueError):
        await QaGenerator().generate(
            gateway,
            question="问题",
            context=ComposedContext([], 0),
        )

    assert gateway.calls == []
