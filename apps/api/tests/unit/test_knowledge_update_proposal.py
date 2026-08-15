from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge_workbench.application.knowledge_update_proposal import (
    KnowledgeUpdateProposalService,
    ProposalCreate,
    ProposalDecision,
    ProposalEdit,
    ProposalEvidenceInput,
    ProposalSubmit,
    ProposalSupersede,
    _clean_list,
    _proposal_snapshot,
    proposal_request_hash,
)
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    KnowledgeUpdateAction,
    KnowledgeUpdateProposalOperation,
    KnowledgeUpdateProposalStatus,
)


def _evidence(role: str = "new_support") -> dict[str, object]:
    return {"role": role, "section_id": str(uuid4())}


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "create",
        "suggested_title": "Title",
        "suggested_body": "Body",
        "evidence": [_evidence()],
    }
    payload.update(overrides)
    return payload


def test_proposal_dtos_forbid_client_audit_and_frozen_fields() -> None:
    with pytest.raises(ValidationError) as caught:
        ProposalCreate.model_validate(_create_payload(actor="spoofed"))
    assert caught.value.errors()[0]["type"] == "extra_forbidden"

    with pytest.raises(ValidationError) as caught:
        ProposalEvidenceInput.model_validate(
            {**_evidence(), "frozen_quote": "forged", "quote_hash": "a" * 64}
        )
    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_revision_evidence_roles_require_matching_revision_reference() -> None:
    revision_id = uuid4()
    assert ProposalEvidenceInput.model_validate(
        {**_evidence("existing_support"), "knowledge_revision_id": str(revision_id)}
    ).knowledge_revision_id == revision_id

    with pytest.raises(ValidationError, match="cannot target a knowledge revision"):
        ProposalEvidenceInput.model_validate(
            {**_evidence("new_support"), "knowledge_revision_id": str(revision_id)}
        )
    with pytest.raises(ValidationError, match="requires a knowledge revision"):
        ProposalEvidenceInput.model_validate(_evidence("conflict"))


def test_create_action_allows_no_target_or_parent_target_but_never_revision() -> None:
    assert ProposalCreate.model_validate(_create_payload()).target_node_id is None
    assert (
        ProposalCreate.model_validate(
            _create_payload(target_node_id=str(uuid4()))
        ).target_revision_id
        is None
    )

    with pytest.raises(ValidationError, match="cannot target an existing revision"):
        ProposalCreate.model_validate(
            _create_payload(target_node_id=str(uuid4()), target_revision_id=str(uuid4()))
        )


def test_existing_knowledge_actions_require_node_and_revision() -> None:
    for action in ("revise", "supersede", "merge_suggestion", "mark_review_recommended"):
        with pytest.raises(ValidationError, match="requires target_node_id"):
            ProposalCreate.model_validate(_create_payload(action=action))

    request = ProposalCreate.model_validate(
        _create_payload(
            action="revise", target_node_id=str(uuid4()), target_revision_id=str(uuid4())
        )
    )
    assert request.action is KnowledgeUpdateAction.REVISE


def test_edit_requires_a_mutable_field_and_positive_version() -> None:
    with pytest.raises(ValidationError, match="At least one proposal field"):
        ProposalEdit.model_validate({"expected_version": 1})
    with pytest.raises(ValidationError):
        ProposalEdit.model_validate({"expected_version": 0, "suggested_body": "Body"})
    assert ProposalEdit(expected_version=1, suggested_body="Body").suggested_body == "Body"


def test_decision_reason_normalizes_blank_text() -> None:
    assert ProposalDecision(expected_version=1, reason=" \t").reason is None
    assert ProposalDecision(expected_version=1, reason="keep").reason == "keep"
    assert ProposalSupersede(
        expected_version=1, replacement_proposal_id=uuid4()
    ).replacement_proposal_id
    assert ProposalSubmit(expected_version=1).expected_version == 1


def test_lists_are_trimmed_deduplicated_and_blank_values_removed() -> None:
    assert _clean_list([" alpha ", "", "alpha", "  ", "beta", " beta "]) == ["alpha", "beta"]


def test_proposal_request_hash_is_canonical_and_operation_scoped() -> None:
    first = ProposalDecision.model_validate({"expected_version": 7, "reason": "approve"})
    second = ProposalDecision.model_validate({"reason": "approve", "expected_version": 7})
    payload = {
        "operation": "approve",
        "request": {"expected_version": 7, "reason": "approve"},
    }
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert proposal_request_hash(KnowledgeUpdateProposalOperation.APPROVE, first) == expected
    assert proposal_request_hash(KnowledgeUpdateProposalOperation.APPROVE, second) == expected
    assert proposal_request_hash(KnowledgeUpdateProposalOperation.REJECT, first) != expected

    first_proposal_id = uuid4()
    second_proposal_id = uuid4()
    assert proposal_request_hash(
        KnowledgeUpdateProposalOperation.APPROVE, first, proposal_id=first_proposal_id
    ) != proposal_request_hash(
        KnowledgeUpdateProposalOperation.APPROVE, first, proposal_id=second_proposal_id
    )


def test_terminal_and_version_guards_are_separate() -> None:
    terminal = SimpleNamespace(version=4, status=KnowledgeUpdateProposalStatus.REJECTED.value)
    with pytest.raises(AppError) as caught:
        KnowledgeUpdateProposalService._ensure_status(terminal, {"pending_review"}, 4)
    assert caught.value.code == "proposal_terminal"

    current = SimpleNamespace(version=5, status=KnowledgeUpdateProposalStatus.DRAFT.value)
    with pytest.raises(AppError) as caught:
        KnowledgeUpdateProposalService._ensure_status(current, {"draft"}, 4)
    assert caught.value.code == "proposal_version_conflict"
    assert caught.value.details == [{"expected_version": 4, "actual_version": 5}]


def test_invalid_status_transition_is_rejected() -> None:
    proposal = SimpleNamespace(version=2, status=KnowledgeUpdateProposalStatus.APPROVED.value)
    with pytest.raises(AppError) as caught:
        KnowledgeUpdateProposalService._ensure_status(proposal, {"draft"}, 2)
    assert caught.value.code == "proposal_invalid_transition"


def test_snapshot_is_detached_from_mutable_model_values() -> None:
    proposal = SimpleNamespace(
        id=uuid4(),
        action="create",
        status="draft",
        version=1,
        target_node_id=None,
        target_revision_id=None,
        target_node_version=None,
        suggested_title="Title",
        suggested_body="Body",
        suggested_tags=["one"],
        conditions=["condition"],
        exceptions=[],
        comparison_summary=None,
        confidence=0.5,
        uncertainty_reason=None,
        superseded_by_proposal_id=None,
    )
    snapshot = _proposal_snapshot(proposal)  # type: ignore[arg-type]
    proposal.suggested_tags.append("two")
    proposal.conditions.append("changed")
    assert snapshot["suggested_tags"] == ["one"]
    assert snapshot["conditions"] == ["condition"]
    assert snapshot["version"] == 1


def test_n21_service_has_no_apply_command_or_indexing_dependency() -> None:
    public_methods = {
        name for name in dir(KnowledgeUpdateProposalService) if not name.startswith("_")
    }
    assert "apply" not in public_methods
    assert "request_knowledge_rebuild" not in public_methods
