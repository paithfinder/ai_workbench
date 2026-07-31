from uuid import UUID

from knowledge_workbench.core.middleware import normalize_request_id


def test_valid_request_id_is_preserved() -> None:
    request_id = "550e8400-e29b-41d4-a716-446655440000"
    assert normalize_request_id(request_id) == request_id


def test_invalid_request_id_is_replaced() -> None:
    assert normalize_request_id("not-valid") != "not-valid"
    UUID(normalize_request_id(None))
