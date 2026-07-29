"""Request metadata and uniform exception handling."""

import logging
import re
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from ai_workbench_api.api.schemas import ApiEnvelope, ApiError, ErrorDetail
from ai_workbench_api.logging import request_id_context

logger = logging.getLogger(__name__)
RequestHandler = Callable[[Request], Awaitable[Response]]
_SAFE_HEADER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _safe_header_id(value: str | None) -> str | None:
    """Accept conservative identifiers and reject path, token, and log injection text."""
    if value and _SAFE_HEADER_ID.fullmatch(value):
        return value
    return None


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach request/correlation identifiers and emit a completion event."""

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        request_id = _safe_header_id(request.headers.get("X-Request-ID")) or str(uuid.uuid4())
        correlation_id = _safe_header_id(request.headers.get("X-Correlation-ID")) or request_id
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        token = request_id_context.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Correlation-ID"] = correlation_id
            logger.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "correlation_id": correlation_id,
                },
            )
            return response
        finally:
            request_id_context.reset(token)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid.uuid4()))


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
) -> JSONResponse:
    """Build the single public error shape."""
    envelope = ApiEnvelope[object](
        error=ErrorDetail(code=code, message=message, details=details),
        request_id=_request_id(request),
    )
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate an expected domain/API error."""
    if not isinstance(exc, ApiError):
        raise TypeError("api_error_handler received an unexpected exception")
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate request validation failures without returning raw input values."""
    if not isinstance(exc, RequestValidationError):
        raise TypeError("validation_error_handler received an unexpected exception")
    details = [
        {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return error_response(
        request,
        status_code=422,
        code="validation_error",
        message="Request validation failed",
        details=details,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unexpected failures and return a non-sensitive error."""
    logger.exception("unhandled request error", exc_info=exc)
    return error_response(
        request,
        status_code=500,
        code="internal_error",
        message="An unexpected error occurred",
    )
