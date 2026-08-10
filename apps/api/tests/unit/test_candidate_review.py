from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from knowledge_workbench.application.candidate_review import (
    CandidateAccept,
    CandidateEdit,
    CandidateNeedsVerification,
    CandidateReject,
    CandidateReviewService,
    ValidatedEvidence,
    _candidate_snapshot,
    _clean_list,
    _required_text,
    review_request_hash,
)
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    CandidateReviewAction,
    CandidateReviewRequest,
    CandidateReviewResult,
    CandidateStatus,
    ParseArtifactStatus,
)


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (CandidateEdit, {"expected_version": 1, "title": "changed", "extra": True}),
        (
            CandidateAccept,
            {"expected_version": 1, "title": "Title", "body": "Body", "extra": True},
        ),
        (
            CandidateNeedsVerification,
            {
                "expected_version": 1,
                "title": "Title",
                "body": "Body",
                "reason": "check source",
                "extra": True,
            },
        ),
        (
            CandidateReject,
            {"expected_version": 1, "title": "Title", "body": "Body", "extra": True},
        ),
    ],
)
def test_review_request_schemas_forbid_unknown_fields(
    schema: type[Any], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as caught:
        schema.model_validate(payload)

    assert caught.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    "schema",
    [CandidateEdit, CandidateAccept, CandidateNeedsVerification, CandidateReject],
)
@pytest.mark.parametrize("expected_version", [0, -1])
def test_review_request_schemas_require_positive_version(
    schema: type[Any], expected_version: int
) -> None:
    payload: dict[str, object] = {"expected_version": expected_version}
    if schema is CandidateEdit:
        payload["title"] = "changed"
    else:
        payload.update(title="Title", body="Body")
    if schema is CandidateNeedsVerification:
        payload["reason"] = "check source"

    with pytest.raises(ValidationError) as caught:
        schema.model_validate(payload)

    assert any(error["loc"] == ("expected_version",) for error in caught.value.errors())


def test_candidate_edit_requires_at_least_one_mutable_field() -> None:
    with pytest.raises(ValidationError, match="At least one candidate field"):
        CandidateEdit(expected_version=1)

    with pytest.raises(ValidationError, match="At least one candidate field"):
        CandidateEdit(expected_version=1, title=None)

    assert CandidateEdit(expected_version=1, suggested_destination_id=None)


def test_client_cannot_supply_review_actor() -> None:
    with pytest.raises(ValidationError) as caught:
        CandidateReject.model_validate(
            {
                "expected_version": 1,
                "title": "Title",
                "body": "Body",
                "actor": "spoofed",
            }
        )

    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_blank_text_and_reason_handling() -> None:
    with pytest.raises(ValidationError, match="reason must not be blank"):
        CandidateNeedsVerification(
            expected_version=1,
            title="Title",
            body="Body",
            reason=" \t\n ",
        )

    assert (
        CandidateReject(expected_version=1, title="Title", body="Body", reason="  ").reason
        is None
    )
    assert (
        CandidateReject(expected_version=1, title="Title", body="Body", reason=None).reason
        is None
    )
    assert (
        CandidateReject(
            expected_version=1,
            title="Title",
            body="Body",
            reason="  duplicate  ",
        ).reason
        == "  duplicate  "
    )

    with pytest.raises(AppError) as caught:
        _required_text(" \t\n ", "title")
    assert caught.value.code == "invalid_candidate_payload"
    assert caught.value.status_code == 422
    assert _required_text("  retained  ", "body") == "retained"


def test_list_fields_are_trimmed_deduplicated_and_blank_values_removed() -> None:
    assert _clean_list([" alpha ", "", "alpha", "  ", "beta", " beta "]) == [
        "alpha",
        "beta",
    ]


def test_request_hash_is_canonical_and_operation_scoped() -> None:
    request = CandidateAccept.model_validate(
        {"title": "Title", "body": "Body", "expected_version": 7}
    )
    payload = {
        "operation": "accept",
        "request": {
            "body": "Body",
            "expected_version": 7,
            "title": "Title",
        },
    }
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert review_request_hash(CandidateReviewAction.ACCEPT, request) == expected
    assert len(expected) == 64
    assert review_request_hash(CandidateReviewAction.EDIT, request) != expected


def test_request_hash_is_stable_across_input_key_order() -> None:
    first = CandidateEdit.model_validate(
        {"expected_version": 2, "title": "Title", "tags": ["one", "two"]}
    )
    second = CandidateEdit.model_validate(
        {"tags": ["one", "two"], "title": "Title", "expected_version": 2}
    )

    assert review_request_hash(CandidateReviewAction.EDIT, first) == review_request_hash(
        CandidateReviewAction.EDIT, second
    )


@pytest.mark.parametrize("status", [CandidateStatus.ACCEPTED.value, CandidateStatus.REJECTED.value])
def test_terminal_candidate_cannot_be_mutated(status: str) -> None:
    candidate = SimpleNamespace(version=4, status=status)

    with pytest.raises(AppError) as caught:
        CandidateReviewService._validate_mutable(candidate, 4)

    assert caught.value.code == "candidate_terminal"
    assert caught.value.status_code == 409


def test_non_terminal_review_statuses_remain_mutable() -> None:
    for status in (
        CandidateStatus.PENDING_REVIEW.value,
        CandidateStatus.NEEDS_VERIFICATION.value,
    ):
        CandidateReviewService._validate_mutable(SimpleNamespace(version=4, status=status), 4)


def test_stale_candidate_version_reports_expected_and_actual() -> None:
    candidate = SimpleNamespace(version=5, status=CandidateStatus.PENDING_REVIEW.value)

    with pytest.raises(AppError) as caught:
        CandidateReviewService._validate_mutable(candidate, 4)

    assert caught.value.code == "candidate_version_conflict"
    assert caught.value.status_code == 409
    assert caught.value.details == [{"expected_version": 4, "actual_version": 5}]


def test_candidate_snapshot_captures_audit_fields_before_mutation() -> None:
    destination_id = uuid4()
    candidate = SimpleNamespace(
        title="Before",
        body="Body",
        tags=["one"],
        suggested_destination_id=destination_id,
        atomicity="atomic",
        conditions=["condition"],
        exceptions=[],
        status=CandidateStatus.PENDING_REVIEW.value,
        verification_reason=None,
        rejection_reason=None,
        version=3,
    )

    snapshot = _candidate_snapshot(candidate)  # type: ignore[arg-type]
    candidate.title = "After"
    candidate.tags.append("two")

    assert snapshot["title"] == "Before"
    assert snapshot["tags"] == ["one"]
    assert snapshot["suggested_destination_id"] == str(destination_id)
    assert snapshot["version"] == 3


def test_review_action_uses_frontend_contract_name() -> None:
    assert (
        CandidateReviewAction.MARK_NEEDS_VERIFICATION.value
        == "mark_needs_verification"
    )


class _EvidenceRows:
    def __init__(self, rows: list[tuple[object, object]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, object]]:
        return self._rows


class _EvidenceSession:
    def __init__(
        self,
        *,
        extraction: object | None,
        version: object | None,
        rows: list[tuple[object, object]],
    ) -> None:
        self.extraction = extraction
        self.version = version
        self.rows = rows

    async def get(self, model: type[object], _identifier: object) -> object | None:
        return self.extraction if model.__name__ == "ExtractionJob" else self.version

    async def execute(self, _statement: object) -> _EvidenceRows:
        return _EvidenceRows(self.rows)


async def test_evidence_requires_current_ready_parse_artifact() -> None:
    candidate = SimpleNamespace(
        id=uuid4(),
        extraction_job_id=uuid4(),
        source_version_id=uuid4(),
        space_id=uuid4(),
    )
    extraction = SimpleNamespace(
        space_id=candidate.space_id,
        source_version_id=candidate.source_version_id,
        parse_artifact_id=uuid4(),
    )
    version = SimpleNamespace(
        current_parse_artifact_id=uuid4(),
        parse_status=ParseArtifactStatus.READY.value,
    )
    session = _EvidenceSession(extraction=extraction, version=version, rows=[])

    with pytest.raises(AppError) as caught:
        await CandidateReviewService._validated_evidence(  # type: ignore[arg-type]
            session, candidate
        )

    assert caught.value.code == "candidate_evidence_stale"


async def test_acceptance_requires_at_least_one_evidence_section() -> None:
    candidate = SimpleNamespace(
        id=uuid4(),
        extraction_job_id=uuid4(),
        source_version_id=uuid4(),
        space_id=uuid4(),
    )
    artifact_id = uuid4()
    extraction = SimpleNamespace(
        space_id=candidate.space_id,
        source_version_id=candidate.source_version_id,
        parse_artifact_id=artifact_id,
    )
    version = SimpleNamespace(
        current_parse_artifact_id=artifact_id,
        parse_status=ParseArtifactStatus.READY.value,
    )
    session = _EvidenceSession(extraction=extraction, version=version, rows=[])

    with pytest.raises(AppError) as caught:
        await CandidateReviewService._validated_evidence(  # type: ignore[arg-type]
            session, candidate
        )

    assert caught.value.code == "candidate_evidence_missing"


async def test_evidence_identity_and_quote_hash_must_match() -> None:
    candidate = SimpleNamespace(
        id=uuid4(),
        extraction_job_id=uuid4(),
        source_version_id=uuid4(),
        space_id=uuid4(),
    )
    artifact_id = uuid4()
    section_id = uuid4()
    extraction = SimpleNamespace(
        space_id=candidate.space_id,
        source_version_id=candidate.source_version_id,
        parse_artifact_id=artifact_id,
    )
    version = SimpleNamespace(
        current_parse_artifact_id=artifact_id,
        parse_status=ParseArtifactStatus.READY.value,
    )
    link = SimpleNamespace(
        source_version_id=candidate.source_version_id,
        quote_hash="a" * 64,
    )
    section = SimpleNamespace(
        id=section_id,
        space_id=candidate.space_id,
        source_version_id=candidate.source_version_id,
        parse_artifact_id=artifact_id,
        quote_hash="b" * 64,
        locator={
            "sourceVersionId": str(candidate.source_version_id),
            "parseArtifactId": str(artifact_id),
            "sectionId": str(section_id),
            "quoteHash": "b" * 64,
        },
    )
    session = _EvidenceSession(extraction=extraction, version=version, rows=[(link, section)])

    with pytest.raises(AppError) as caught:
        await CandidateReviewService._validated_evidence(  # type: ignore[arg-type]
            session, candidate
        )

    assert caught.value.code == "candidate_evidence_stale"


async def test_matching_evidence_identity_is_returned() -> None:
    candidate = SimpleNamespace(
        id=uuid4(),
        extraction_job_id=uuid4(),
        source_version_id=uuid4(),
        space_id=uuid4(),
    )
    artifact_id = uuid4()
    section_id = uuid4()
    quote_hash = "c" * 64
    extraction = SimpleNamespace(
        space_id=candidate.space_id,
        source_version_id=candidate.source_version_id,
        parse_artifact_id=artifact_id,
    )
    version = SimpleNamespace(
        current_parse_artifact_id=artifact_id,
        parse_status=ParseArtifactStatus.READY.value,
    )
    link = SimpleNamespace(
        source_version_id=candidate.source_version_id,
        quote_hash=quote_hash,
    )
    section = SimpleNamespace(
        id=section_id,
        space_id=candidate.space_id,
        source_version_id=candidate.source_version_id,
        parse_artifact_id=artifact_id,
        quote_hash=quote_hash,
        locator={
            "sourceVersionId": str(candidate.source_version_id),
            "parseArtifactId": str(artifact_id),
            "sectionId": str(section_id),
            "quoteHash": quote_hash,
        },
    )
    session = _EvidenceSession(extraction=extraction, version=version, rows=[(link, section)])

    validated = await CandidateReviewService._validated_evidence(  # type: ignore[arg-type]
        session, candidate
    )

    assert validated == [ValidatedEvidence(link, section)]


class _ExecuteResult:
    def __init__(self, row: tuple[CandidateReviewRequest, CandidateReviewResult] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[CandidateReviewRequest, CandidateReviewResult] | None:
        return self._row


class _ReplaySession:
    def __init__(self, row: tuple[CandidateReviewRequest, CandidateReviewResult] | None) -> None:
        self.row = row
        self.execute_calls = 0

    async def execute(self, _statement: object) -> _ExecuteResult:
        self.execute_calls += 1
        return _ExecuteResult(self.row)


def _stored_replay(request_hash: str) -> tuple[CandidateReviewRequest, CandidateReviewResult]:
    request_id = uuid4()
    review_id = uuid4()
    request = CandidateReviewRequest(
        id=request_id,
        candidate_id=uuid4(),
        space_id=uuid4(),
        idempotency_key="review-key",
        operation=CandidateReviewAction.REJECT.value,
        request_hash=request_hash,
        expected_version=3,
    )
    result = CandidateReviewResult(
        id=uuid4(),
        request_id=request_id,
        candidate_review_id=review_id,
        candidate_version=4,
        candidate_status=CandidateStatus.REJECTED.value,
        knowledge_node_id=None,
        knowledge_revision_id=None,
        review_card_id=None,
        evidence_ids=[],
    )
    return request, result


async def test_matching_idempotency_replay_returns_the_stored_result_exactly() -> None:
    request_hash = "a" * 64
    request_row, result_row = _stored_replay(request_hash)
    candidate_id = request_row.candidate_id
    session = _ReplaySession((request_row, result_row))

    outcome = await CandidateReviewService()._replay(
        session,  # type: ignore[arg-type]
        space_id=request_row.space_id,
        candidate_id=candidate_id,
        key=request_row.idempotency_key,
        request_hash=request_hash,
    )

    assert outcome is not None
    assert outcome.result_id == result_row.id
    assert outcome.candidate_review_id == result_row.candidate_review_id
    assert outcome.candidate_id == candidate_id
    assert outcome.candidate_version == 4
    assert outcome.candidate_status == CandidateStatus.REJECTED.value
    assert outcome.knowledge_node_id is None
    assert outcome.evidence_ids == []
    assert session.execute_calls == 1


async def test_matching_idempotency_replay_bypasses_terminal_candidate_lock() -> None:
    request_hash = "f" * 64
    request_row, result_row = _stored_replay(request_hash)

    class ReplayFirstService(CandidateReviewService):
        async def _lock_candidate(self, *_args: object) -> object:
            raise AssertionError("matching replay must not lock the terminal candidate")

    outcome = await ReplayFirstService()._replay(
        _ReplaySession((request_row, result_row)),  # type: ignore[arg-type]
        space_id=request_row.space_id,
        candidate_id=request_row.candidate_id,
        key=request_row.idempotency_key,
        request_hash=request_hash,
    )

    assert outcome is not None
    assert outcome.result_id == result_row.id


async def test_reusing_idempotency_key_with_different_request_is_a_conflict() -> None:
    request_row, result_row = _stored_replay("a" * 64)
    session = _ReplaySession((request_row, result_row))

    with pytest.raises(AppError) as caught:
        await CandidateReviewService()._replay(
            session,  # type: ignore[arg-type]
            space_id=request_row.space_id,
            candidate_id=request_row.candidate_id,
            key=request_row.idempotency_key,
            request_hash="b" * 64,
        )

    assert caught.value.code == "idempotency_conflict"
    assert caught.value.status_code == 409


async def test_unknown_idempotency_key_is_not_a_replay() -> None:
    session = _ReplaySession(None)

    assert (
        await CandidateReviewService()._replay(
            session,  # type: ignore[arg-type]
            space_id=uuid4(),
            candidate_id=uuid4(),
            key="new-key",
            request_hash="c" * 64,
        )
        is None
    )


async def test_reconciliation_get_returns_durable_request_and_result() -> None:
    request_row, result_row = _stored_replay("e" * 64)
    session = _ReplaySession((request_row, result_row))

    stored, outcome = await CandidateReviewService().get_request(
        session,  # type: ignore[arg-type]
        space_id=request_row.space_id,
        candidate_id=request_row.candidate_id,
        idempotency_key=request_row.idempotency_key,
    )

    assert stored is request_row
    assert outcome.result_id == result_row.id
    assert outcome.candidate_review_id == result_row.candidate_review_id


async def test_reconciliation_get_rejects_unknown_key() -> None:
    session = _ReplaySession(None)

    with pytest.raises(AppError) as caught:
        await CandidateReviewService().get_request(
            session,  # type: ignore[arg-type]
            space_id=uuid4(),
            candidate_id=uuid4(),
            idempotency_key="missing-key",
        )

    assert caught.value.code == "candidate_review_request_not_found"
    assert caught.value.status_code == 404


def test_replay_outcome_restores_uuid_evidence_ids() -> None:
    evidence_id = uuid4()
    _, result = _stored_replay("d" * 64)
    result.evidence_ids = [str(evidence_id)]

    outcome = CandidateReviewService._outcome(result, uuid4())

    assert outcome.evidence_ids == [evidence_id]
    assert isinstance(outcome.evidence_ids[0], UUID)
