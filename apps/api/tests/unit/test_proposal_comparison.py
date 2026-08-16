from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge_workbench.application.proposal_comparison import (
    ProposalComparisonRequest,
    ProposalComparisonService,
    proposal_comparison_request_hash,
)
from knowledge_workbench.application.proposal_comparison_generation import (
    ProposalComparisonGenerationOutput,
)
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    KnowledgeUpdateAction,
    KnowledgeUpdateProposalEvidenceRole,
    ProposalComparisonCandidateKind,
)


def _evidence_backed_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "comparison_kind": "support",
        "suggested_action": "create",
        "summary": "New evidence supports a draft proposal.",
        "new_evidence_ids": ["N1"],
    }
    payload.update(overrides)
    return payload


def test_comparison_request_rejects_unknown_fields_and_hash_is_canonical() -> None:
    with pytest.raises(ValidationError):
        ProposalComparisonRequest.model_validate(
            {
                "selection_batch_id": str(uuid4()),
                "scope": {},
                "unexpected": True,
            }
        )

    request_id = uuid4()
    batch_id = uuid4()
    first = ProposalComparisonRequest.model_validate(
        {"selection_batch_id": str(batch_id), "scope": {"include_descendants": True}}
    )
    second = ProposalComparisonRequest.model_validate(
        {"scope": {"include_descendants": True}, "selection_batch_id": str(batch_id)}
    )

    assert proposal_comparison_request_hash(research_run_id=request_id, request=first) == (
        proposal_comparison_request_hash(research_run_id=request_id, request=second)
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"comparison_kind": "no_evidence", "summary": "Insufficient evidence."},
        _evidence_backed_payload(new_evidence_ids=[]),
        _evidence_backed_payload(comparison_kind="conflict"),
        _evidence_backed_payload(comparison_kind="duplicate", existing_evidence_ids=[]),
        _evidence_backed_payload(new_evidence_ids=["N1", "N1"]),
        _evidence_backed_payload(suggested_tags=[" duplicate ", "duplicate"]),
    ],
)
def test_generation_output_rejects_invalid_evidence_shapes(payload: object) -> None:
    with pytest.raises(ValidationError):
        ProposalComparisonGenerationOutput.model_validate(payload)


def test_no_evidence_requires_only_summary_and_uncertainty_reason() -> None:
    output = ProposalComparisonGenerationOutput.model_validate(
        {
            "comparison_kind": "no_evidence",
            "summary": "The selected sources do not support a safe comparison.",
            "uncertainty_reason": "No comparable confirmed knowledge was available.",
        }
    )

    assert output.new_evidence_ids == []
    assert output.existing_evidence_ids == []
    assert output.suggested_action is None

    with pytest.raises(ValidationError):
        ProposalComparisonGenerationOutput.model_validate(
            {
                "comparison_kind": "no_evidence",
                "summary": "Insufficient evidence.",
                "uncertainty_reason": "No match.",
                "new_evidence_ids": ["N1"],
            }
        )


def test_candidate_ids_reject_forgery_duplicate_and_role_confusion() -> None:
    new = SimpleNamespace(
        candidate_id="N1",
        candidate_kind=ProposalComparisonCandidateKind.NEW_SOURCE.value,
    )
    existing = SimpleNamespace(
        candidate_id="E1",
        candidate_kind=ProposalComparisonCandidateKind.EXISTING_EVIDENCE.value,
    )
    candidates = {"N1": new, "E1": existing}

    assert ProposalComparisonService._candidate_ids(  # noqa: SLF001
        ["N1"],
        candidates,
        ProposalComparisonCandidateKind.NEW_SOURCE,
    ) == [new]

    for identifiers, kind in (
        (["N1", "N1"], ProposalComparisonCandidateKind.NEW_SOURCE),
        (["forged"], ProposalComparisonCandidateKind.NEW_SOURCE),
        (["E1"], ProposalComparisonCandidateKind.NEW_SOURCE),
    ):
        with pytest.raises(AppError) as caught:
            ProposalComparisonService._candidate_ids(  # noqa: SLF001
                identifiers,
                candidates,
                kind,
            )
        assert caught.value.code == "proposal_comparison_evidence_invalid"
        assert caught.value.status_code == 409


def test_candidate_context_budget_persists_inclusion_and_hides_excluded_text() -> None:
    candidates = [
        SimpleNamespace(
            candidate_id="N1",
            candidate_kind=ProposalComparisonCandidateKind.NEW_SOURCE.value,
            title="New",
            frozen_quote="abcdefgh",
            included_in_context=False,
            context_ordinal=None,
        ),
        SimpleNamespace(
            candidate_id="E1",
            candidate_kind=ProposalComparisonCandidateKind.EXISTING_EVIDENCE.value,
            title="Existing",
            frozen_quote="ijklmnop",
            included_in_context=True,
            context_ordinal=0,
        ),
        SimpleNamespace(
            candidate_id="N2",
            candidate_kind=ProposalComparisonCandidateKind.NEW_SOURCE.value,
            title="Excluded",
            frozen_quote="qrstuvwx",
            included_in_context=True,
            context_ordinal=1,
        ),
    ]

    context = ProposalComparisonService._apply_context_budget(  # noqa: SLF001
        candidates,
        max_chunks=2,
        max_characters=10,
        max_chunk_characters=6,
    )

    assert context == {"N1": "abcdef", "E1": "ijkl"}
    assert [(item.included_in_context, item.context_ordinal) for item in candidates] == [
        (True, 0),
        (True, 1),
        (False, None),
    ]
    prompt = ProposalComparisonService._candidate_prompt(candidates, context)  # noqa: SLF001
    assert "abcdefgh" not in prompt
    assert "qrstuvwx" not in prompt
    assert "[N1]" in prompt
    assert "[E1]" in prompt


def test_duplicate_forces_review_action_and_existing_roles_are_stable() -> None:
    service = ProposalComparisonService()

    assert (
        service._action(  # noqa: SLF001
            "duplicate",
            KnowledgeUpdateAction.CREATE,
            has_existing=True,
        )
        is KnowledgeUpdateAction.MARK_REVIEW_RECOMMENDED
    )
    assert service._existing_role("conflict") is KnowledgeUpdateProposalEvidenceRole.CONFLICT  # noqa: SLF001
    assert service._existing_role("outdated") is KnowledgeUpdateProposalEvidenceRole.OUTDATED  # noqa: SLF001
    assert (  # noqa: SLF001
        service._existing_role("support") is KnowledgeUpdateProposalEvidenceRole.EXISTING_SUPPORT
    )


def test_comparison_routes_expose_only_expected_operations() -> None:
    from knowledge_workbench.config import Settings
    from knowledge_workbench.main import create_app

    paths = create_app(Settings(app_env="test")).openapi()["paths"]
    create_path = (
        "/api/v1/knowledge-spaces/{space_id}/research-runs/{research_run_id}/proposal-comparisons"
    )
    get_path = "/api/v1/knowledge-spaces/{space_id}/proposal-comparisons/{comparison_id}"

    assert set(paths[create_path]) == {"post"}
    assert set(paths[get_path]) == {"get"}
    assert paths[create_path]["post"]["operationId"] == "create_proposal_comparison"
    assert paths[get_path]["get"]["operationId"] == "get_proposal_comparison"
    assert "Idempotency-Key" in {
        parameter["name"] for parameter in paths[create_path]["post"]["parameters"]
    }
