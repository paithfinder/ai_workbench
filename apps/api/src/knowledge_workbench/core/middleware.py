from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import structlog
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from knowledge_workbench.core.errors import error_response
from knowledge_workbench.core.request_id import reset_request_id, set_request_id

REQUEST_ID_HEADER = "X-Request-ID"
_PASTED_TEXT_PATH_SUFFIX = "/sources/pasted-text"
_JSON_ENVELOPE_ALLOWANCE = 4096


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


def pasted_text_body_limit_middleware(
    max_pasted_text_bytes: int,
) -> Callable[
    [Request, Callable[[Request], Awaitable[Response]]],
    Awaitable[Response],
]:
    max_request_bytes = max_pasted_text_bytes + _JSON_ENVELOPE_ALLOWANCE

    async def middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method != "POST" or not request.url.path.endswith(
            _PASTED_TEXT_PATH_SUFFIX
        ):
            return await call_next(request)
        raw_length = request.headers.get("content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                return error_response(
                    code="invalid_content_length",
                    message="Content-Length must be a non-negative integer.",
                    status_code=422,
                )
            if declared_length < 0:
                return error_response(
                    code="invalid_content_length",
                    message="Content-Length must be a non-negative integer.",
                    status_code=422,
                )
            if declared_length > max_request_bytes:
                return error_response(
                    code="pasted_text_too_large",
                    message="Pasted text exceeds the configured size limit.",
                    status_code=413,
                )

        received = 0
        original_receive = request.receive

        async def limited_receive() -> Message:
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    received += len(body)
                    if received > max_request_bytes:
                        raise _RequestBodyTooLarge
            return message

        request._receive = limited_receive  # noqa: SLF001
        try:
            return await call_next(request)
        except _RequestBodyTooLarge:
            return error_response(
                code="pasted_text_too_large",
                message="Pasted text exceeds the configured size limit.",
                status_code=413,
            )

    return middleware


class _RequestBodyTooLarge(Exception):
    pass
