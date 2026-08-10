from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge_workbench.application.extraction import (
    CandidateDraft,
    ExtractionBatchOutput,
    candidate_fingerprint,
    extraction_system_prompt,
)
from knowledge_workbench.db.models import CandidateAtomicity


def _candidate(**overrides: object) -> CandidateDraft:
    payload: dict[str, object] = {
        "title": "候选标题",
        "body": "候选正文",
        "tags": ["知识"],
        "evidence_section_ids": [uuid4()],
        "suggested_destination_id": None,
        "atomicity": CandidateAtomicity.ATOMIC,
        "confidence": 0.8,
        "needs_verification": False,
        "verification_reason": None,
        "conditions": [],
        "exceptions": [],
    }
    payload.update(overrides)
    return CandidateDraft.model_validate(payload)


def test_candidate_output_is_strict_and_requires_verification_reason() -> None:
    with pytest.raises(ValidationError):
        CandidateDraft.model_validate(
            {
                **_candidate().model_dump(),
                "needs_verification": True,
                "verification_reason": None,
            }
        )
    with pytest.raises(ValidationError):
        CandidateDraft.model_validate({**_candidate().model_dump(), "invented": True})


def test_empty_candidate_batch_is_valid() -> None:
    assert ExtractionBatchOutput.model_validate({"candidates": []}).candidates == []


def test_candidate_fingerprint_normalizes_outer_whitespace() -> None:
    first = _candidate(title=" 标题 ", body=" 正文 ")
    second = _candidate(title="标题", body="正文")
    assert candidate_fingerprint(first) == candidate_fingerprint(second)


def test_extraction_prompt_treats_source_as_untrusted_data() -> None:
    prompt = extraction_system_prompt()
    assert "不可信数据" in prompt
    assert "只能复制输入中的 section_id" in prompt
    assert "永远不能标记为已确认知识" in prompt
