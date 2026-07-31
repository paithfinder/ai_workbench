from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import structlog
from starlette.requests import Request
from starlette.responses import Response

from knowledge_workbench.core.request_id import reset_request_id, set_request_id

REQUEST_ID_HEADER = "X-Request-ID"


def normalize_request_id(value: str | None) -> str:
    if value is not None:
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return str(uuid4())


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
    token = set_request_id(request_id)
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        structlog.contextvars.unbind_contextvars("request_id")
        reset_request_id(token)
