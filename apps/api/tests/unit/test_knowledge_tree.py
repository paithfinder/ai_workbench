from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from knowledge_workbench.api.bootstrap import CapabilityResponse
from knowledge_workbench.api.knowledge_tree import router
from knowledge_workbench.application.knowledge_tree import (
    CREATABLE_KINDS,
    LEGAL_PARENT_KINDS,
    KnowledgeNodeCreate,
    KnowledgeNodeDelete,
    KnowledgeNodeEdit,
    KnowledgeNodeMove,
    KnowledgeTreeService,
    KnowledgeWriteOperation,
    _is_same_or_descendant,
    _path_label,
    _replace_path_prefix,
    knowledge_request_hash,
    revision_content_hash,
)
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import (
    KnowledgeNodeKind,
    KnowledgeWriteRequest,
    KnowledgeWriteResult,
)


def test_d6_capability_is_enabled() -> None:
    assert CapabilityResponse().knowledge_tree is True


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (
            KnowledgeNodeCreate,
            {
                "expected_version": 1,
                "kind": "folder",
                "title": "Folder",
                "extra": True,
            },
        ),
        (
            KnowledgeNodeEdit,
            {
                "expected_version": 1,
                "expected_revision_id": str(uuid4()),
                "title": "Changed",
                "extra": True,
            },
        ),
        (
            KnowledgeNodeMove,
            {"expected_version": 1, "parent_id": str(uuid4()), "extra": True},
        ),
        (KnowledgeNodeDelete, {"expected_version": 1, "extra": True}),
    ],
)
def test_write_schemas_forbid_unknown_fields(
    schema: type[Any], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as caught:
        schema.model_validate(payload)

    assert caught.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (
            KnowledgeNodeCreate,
            {"expected_version": 0, "kind": "folder", "title": "Folder"},
        ),
        (
            KnowledgeNodeEdit,
            {
                "expected_version": 0,
                "expected_revision_id": str(uuid4()),
                "title": "Changed",
            },
        ),
        (KnowledgeNodeMove, {"expected_version": 0}),
        (KnowledgeNodeDelete, {"expected_version": 0}),
    ],
)
def test_write_schemas_require_positive_expected_version(
    schema: type[Any], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as caught:
        schema.model_validate(payload)

    assert any(error["loc"] == ("expected_version",) for error in caught.value.errors())


def test_edit_requires_expected_revision_and_a_revision_change() -> None:
    with pytest.raises(ValidationError):
        KnowledgeNodeEdit(expected_version=1, title="Changed")  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="At least one revision field"):
        KnowledgeNodeEdit(
            expected_version=1,
            expected_revision_id=uuid4(),
            reason="audit only",
        )

    with pytest.raises(ValidationError, match="At least one revision field"):
        KnowledgeNodeEdit(
            expected_version=1,
            expected_revision_id=uuid4(),
            title=None,
        )


def test_create_rejects_root_source_and_point_at_service_boundary() -> None:
    # The enum accepts durable node kinds so the service can return a domain conflict.
    assert (
        KnowledgeNodeCreate(
            expected_version=1,
            kind=KnowledgeNodeKind.ROOT,
            title="Root",
        ).kind
        is KnowledgeNodeKind.ROOT
    )
    assert (
        KnowledgeNodeCreate(
            expected_version=1,
            kind=KnowledgeNodeKind.SOURCE,
            title="Source",
        ).kind
        is KnowledgeNodeKind.SOURCE
    )
    assert KnowledgeNodeKind.POINT.value not in CREATABLE_KINDS


def test_request_hash_is_canonical_operation_and_node_scoped() -> None:
    node_id = uuid4()
    request = KnowledgeNodeEdit.model_validate(
        {
            "title": "Title",
            "expected_revision_id": str(uuid4()),
            "expected_version": 7,
            "tags": ["one", "two"],
        }
    )
    payload = {
        "node_id": str(node_id),
        "operation": "edit",
        "request": request.model_dump(mode="json", exclude_unset=True),
    }
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert knowledge_request_hash(
        KnowledgeWriteOperation.EDIT, request, node_id=node_id
    ) == expected
    assert len(expected) == 64
    assert (
        knowledge_request_hash(KnowledgeWriteOperation.EDIT, request, node_id=uuid4())
        != expected
    )
    assert (
        knowledge_request_hash(KnowledgeWriteOperation.MOVE, request, node_id=node_id)
        != expected
    )


def test_request_hash_is_stable_across_input_key_order() -> None:
    revision_id = uuid4()
    first = KnowledgeNodeEdit.model_validate(
        {
            "expected_version": 2,
            "expected_revision_id": revision_id,
            "title": "Title",
            "tags": ["one", "two"],
        }
    )
    second = KnowledgeNodeEdit.model_validate(
        {
            "tags": ["one", "two"],
            "title": "Title",
            "expected_revision_id": revision_id,
            "expected_version": 2,
        }
    )

    assert knowledge_request_hash(KnowledgeWriteOperation.EDIT, first) == (
        knowledge_request_hash(KnowledgeWriteOperation.EDIT, second)
    )


def test_migration_and_runtime_revision_hashes_match() -> None:
    migration_path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0006_d6_knowledge_tree.py"
    )
    spec = importlib.util.spec_from_file_location("d6_knowledge_tree", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    fields = {
        "title": "标题",
        "body": "正文",
        "tags": ["标签"],
        "conditions": ["条件"],
        "exceptions": ["例外"],
    }

    assert migration._revision_content_hash(**fields) == revision_content_hash(**fields)


def test_migration_deterministic_ids_are_rfc_uuid5() -> None:
    migration_path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0006_d6_knowledge_tree.py"
    )
    spec = importlib.util.spec_from_file_location("d6_knowledge_tree_uuid", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert "'5'" in migration.ROOT_UUID_SQL
    assert "'8'" in migration.ROOT_UUID_SQL
    assert "'5'" in migration.ROOT_REVISION_UUID_SQL
    assert "'8'" in migration.ROOT_REVISION_UUID_SQL
    assert "'5'" in migration.SOURCE_UUID_SQL
    assert "'8'" in migration.SOURCE_UUID_SQL
    assert "md5('knowledge-root:' || id::text)::uuid" not in migration.ROOT_UUID_SQL


def test_revision_content_hash_covers_all_revision_fields() -> None:
    base = revision_content_hash(
        title="Title",
        body="Body",
        tags=["tag"],
        conditions=["condition"],
        exceptions=["exception"],
    )

    assert len(base) == 64
    assert base != revision_content_hash(
        title="Title",
        body="Changed",
        tags=["tag"],
        conditions=["condition"],
        exceptions=["exception"],
    )
    assert base != revision_content_hash(
        title="Title",
        body="Body",
        tags=["tag"],
        conditions=["other"],
        exceptions=["exception"],
    )


@pytest.mark.parametrize(
    ("kind", "parent_kind"),
    [
        ("folder", "root"),
        ("folder", "folder"),
        ("document", "root"),
        ("document", "folder"),
        ("point", "document"),
    ],
)
def test_legal_parent_types_are_accepted(kind: str, parent_kind: str) -> None:
    KnowledgeTreeService._validate_parent(kind, SimpleNamespace(kind=parent_kind))  # type: ignore[arg-type]
    assert parent_kind in LEGAL_PARENT_KINDS[kind]


@pytest.mark.parametrize(
    ("kind", "parent_kind"),
    [
        ("folder", "document"),
        ("document", "document"),
        ("point", "folder"),
        ("point", "point"),
    ],
)
def test_illegal_parent_types_are_conflicts(kind: str, parent_kind: str) -> None:
    with pytest.raises(AppError) as caught:
        KnowledgeTreeService._validate_parent(  # type: ignore[arg-type]
            kind, SimpleNamespace(kind=parent_kind)
        )

    assert caught.value.code == "invalid_knowledge_parent"
    assert caught.value.status_code == 409


@pytest.mark.parametrize("kind", ["root", "source"])
def test_root_and_source_are_immutable(kind: str) -> None:
    with pytest.raises(AppError) as caught:
        KnowledgeTreeService._validate_mutable(SimpleNamespace(kind=kind))  # type: ignore[arg-type]

    assert caught.value.code == "immutable_knowledge_node"
    assert caught.value.status_code == 409


def test_stale_node_version_reports_expected_and_actual() -> None:
    with pytest.raises(AppError) as caught:
        KnowledgeTreeService._validate_version(  # type: ignore[arg-type]
            SimpleNamespace(version=5), 4
        )

    assert caught.value.code == "knowledge_node_version_conflict"
    assert caught.value.details == [{"expected_version": 4, "actual_version": 5}]


def test_ltree_path_helpers_preserve_subtree_suffix() -> None:
    node_id = UUID("01982ba0-4f20-7000-8000-000000000123")
    assert _path_label(node_id) == "n01982ba04f2070008000000000000123"
    assert _is_same_or_descendant("nroot.nfolder.nchild", "nroot.nfolder")
    assert not _is_same_or_descendant("nroot.nfolder2", "nroot.nfolder")
    assert (
        _replace_path_prefix(
            "nroot.nold.nchild.ngrandchild",
            "nroot.nold",
            "nroot.nnew.nold",
        )
        == "nroot.nnew.nold.nchild.ngrandchild"
    )


def test_evidence_query_freezes_exact_identity_and_space() -> None:
    statement = KnowledgeTreeService._evidence_statement(space_id=uuid4())
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "knowledge_nodes.deleted_at IS NULL" in sql
    assert "source_parse_artifacts.status =" in sql
    assert (
        "source_parse_artifacts.source_version_id = "
        "knowledge_evidence.source_version_id" in sql
    )
    assert "source_sections.parse_artifact_id = knowledge_evidence.parse_artifact_id" in sql
    assert "source_sections.quote_hash = knowledge_evidence.quote_hash" in sql
    assert "source_sections.content_hash = knowledge_evidence.content_hash" in sql
    assert "sources.space_id =" in sql


class _SearchResult:
    def all(self) -> list[object]:
        return []


class _SearchSession:
    def __init__(self) -> None:
        self.statement: object | None = None

    async def execute(self, statement: object) -> _SearchResult:
        self.statement = statement
        return _SearchResult()


async def test_search_query_uses_ordered_paths_and_current_evidence_sources() -> None:
    session = _SearchSession()

    assert await KnowledgeTreeService().search(
        session,  # type: ignore[arg-type]
        space_id=uuid4(),
        query="source",
    ) == []
    assert session.statement is not None
    sql = str(session.statement.compile(dialect=postgresql.dialect()))  # type: ignore[union-attr]

    assert "ORDER BY knowledge_ancestor.path" in sql
    assert "knowledge_nodes.current_revision_id" in sql
    assert "knowledge_evidence_source_titles.source_titles ILIKE" in sql
    assert "knowledge_evidence.space_id = knowledge_nodes.space_id" in sql
    assert "sources.space_id =" in sql


class _ExecuteResult:
    def __init__(
        self, row: tuple[KnowledgeWriteRequest, KnowledgeWriteResult] | None
    ) -> None:
        self._row = row

    def one_or_none(self) -> tuple[KnowledgeWriteRequest, KnowledgeWriteResult] | None:
        return self._row


class _ReplaySession:
    def __init__(
        self, row: tuple[KnowledgeWriteRequest, KnowledgeWriteResult] | None
    ) -> None:
        self.row = row
        self.execute_calls = 0

    async def execute(self, _statement: object) -> _ExecuteResult:
        self.execute_calls += 1
        return _ExecuteResult(self.row)


def _stored_replay(
    request_hash: str,
) -> tuple[KnowledgeWriteRequest, KnowledgeWriteResult]:
    request_id = uuid4()
    node_id = uuid4()
    request = KnowledgeWriteRequest(
        id=request_id,
        space_id=uuid4(),
        idempotency_key="write-key",
        operation=KnowledgeWriteOperation.DELETE.value,
        request_hash=request_hash,
    )
    result = KnowledgeWriteResult(
        id=uuid4(),
        request_id=request_id,
        node_id=node_id,
        node_version=4,
        snapshot={"node": {"id": str(node_id), "deleted_at": "2026-08-05T00:00:00Z"}},
    )
    return request, result


async def test_matching_replay_returns_durable_result() -> None:
    request_hash = "a" * 64
    request_row, result_row = _stored_replay(request_hash)
    session = _ReplaySession((request_row, result_row))

    outcome = await KnowledgeTreeService()._replay(
        session,  # type: ignore[arg-type]
        space_id=request_row.space_id,
        key=request_row.idempotency_key,
        request_hash=request_hash,
    )

    assert outcome is not None
    assert outcome.result_id == result_row.id
    assert outcome.request_id == request_row.id
    assert outcome.node_id == result_row.node_id
    assert outcome.node_version == 4
    assert outcome.snapshot == result_row.snapshot
    assert session.execute_calls == 1


async def test_reusing_key_with_different_request_is_conflict() -> None:
    request_row, result_row = _stored_replay("a" * 64)

    with pytest.raises(AppError) as caught:
        await KnowledgeTreeService()._replay(
            _ReplaySession((request_row, result_row)),  # type: ignore[arg-type]
            space_id=request_row.space_id,
            key=request_row.idempotency_key,
            request_hash="b" * 64,
        )

    assert caught.value.code == "idempotency_conflict"
    assert caught.value.status_code == 409


async def test_reconciliation_get_is_space_scoped_and_returns_stored_result() -> None:
    request_row, result_row = _stored_replay("c" * 64)

    stored, outcome = await KnowledgeTreeService().get_write_request(
        _ReplaySession((request_row, result_row)),  # type: ignore[arg-type]
        space_id=request_row.space_id,
        idempotency_key=request_row.idempotency_key,
    )

    assert stored is request_row
    assert outcome.result_id == result_row.id


async def test_reconciliation_get_rejects_unknown_key() -> None:
    with pytest.raises(AppError) as caught:
        await KnowledgeTreeService().get_write_request(
            _ReplaySession(None),  # type: ignore[arg-type]
            space_id=uuid4(),
            idempotency_key="missing-key",
        )

    assert caught.value.code == "knowledge_write_request_not_found"
    assert caught.value.status_code == 404


class _SpaceLockResult:
    pass


class _SpaceLockSession:
    def __init__(self) -> None:
        self.statements: list[object] = []
        self.space = SimpleNamespace(id=uuid4())

    async def execute(self, statement: object) -> _SpaceLockResult:
        self.statements.append(statement)
        return _SpaceLockResult()

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return self.space


async def test_tree_space_lock_matches_candidate_accept_lock_order() -> None:
    session = _SpaceLockSession()
    space_id = session.space.id

    assert await KnowledgeTreeService._lock_space(  # type: ignore[arg-type]
        session, space_id
    ) is session.space
    assert len(session.statements) == 2
    advisory_sql = str(
        session.statements[0].compile(dialect=postgresql.dialect())  # type: ignore[union-attr]
    )
    row_lock_sql = str(
        session.statements[1].compile(dialect=postgresql.dialect())  # type: ignore[union-attr]
    )
    assert "pg_advisory_xact_lock" in advisory_sql
    assert "hashtext" in advisory_sql
    assert "FOR UPDATE" in row_lock_sql


def test_router_exposes_exact_required_endpoints() -> None:
    routes = {
        (method, route.path)
        for route in router.routes
        for method in route.methods or set()
    }
    prefix = "/api/v1/knowledge-spaces/{space_id}"

    assert routes == {
        ("GET", f"{prefix}/knowledge-tree"),
        ("GET", f"{prefix}/knowledge-nodes/{{node_id}}"),
        ("GET", f"{prefix}/knowledge-search"),
        ("POST", f"{prefix}/knowledge-nodes"),
        ("PATCH", f"{prefix}/knowledge-nodes/{{node_id}}"),
        ("POST", f"{prefix}/knowledge-nodes/{{node_id}}/move"),
        ("DELETE", f"{prefix}/knowledge-nodes/{{node_id}}"),
        ("GET", f"{prefix}/knowledge-write-requests/{{idempotency_key}}"),
        ("GET", f"{prefix}/knowledge-evidence/{{evidence_id}}"),
    }


def test_all_write_routes_require_idempotency_header() -> None:
    write_routes = [
        route
        for route in router.routes
        if (route.methods or set()) & {"POST", "PATCH", "DELETE"}
    ]

    assert len(write_routes) == 4
    for route in write_routes:
        header_names = {
            parameter.alias
            for parameter in route.dependant.header_params  # type: ignore[attr-defined]
        }
        assert "Idempotency-Key" in header_names
