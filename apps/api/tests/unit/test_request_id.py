from uuid import UUID

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from knowledge_workbench.core.middleware import (
    normalize_request_id,
    pasted_text_body_limit_middleware,
)


def test_valid_request_id_is_preserved() -> None:
    request_id = "550e8400-e29b-41d4-a716-446655440000"
    assert normalize_request_id(request_id) == request_id


def test_invalid_request_id_is_replaced() -> None:
    assert normalize_request_id("not-valid") != "not-valid"
    UUID(normalize_request_id(None))


async def test_pasted_text_body_limit_rejects_declared_overflow() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/knowledge-spaces/space/sources/pasted-text",
            "headers": [(b"content-length", b"5000")],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
        }
    )
    called = False

    async def call_next(_: Request) -> Response:
        nonlocal called
        called = True
        return Response()

    response = await pasted_text_body_limit_middleware(10)(request, call_next)

    assert response.status_code == 413
    assert not called


async def test_pasted_text_body_limit_rejects_streaming_overflow() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]
    )

    async def receive() -> Message:
        return next(messages)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/knowledge-spaces/space/sources/pasted-text",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
        },
        receive,
    )

    async def call_next(value: Request) -> Response:
        await value.receive()
        await value.receive()
        return Response()

    response = await pasted_text_body_limit_middleware(-4090)(request, call_next)

    assert response.status_code == 413
