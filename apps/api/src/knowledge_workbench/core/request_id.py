from __future__ import annotations

from contextvars import ContextVar, Token

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(value: str) -> Token[str | None]:
    return request_id_context.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    request_id_context.reset(token)


def get_request_id() -> str:
    return request_id_context.get() or "unknown"
