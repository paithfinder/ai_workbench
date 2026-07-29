"""Structured JSON logging with credential redaction."""

import json
import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[-_]?key|access[-_]?token|client[-_]?secret|password|"
    r"preview[-_]?token|request[-_]?body|response[-_]?body|canonical[-_]?path|"
    r"root[-_]?path|submitted[-_]?path|absolute[-_]?path)",
    re.IGNORECASE,
)
_SAFE_LOG_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsecret_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bntn_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/])[^\r\n]*"
    ),
    re.compile(r"(?<![\\/])(?:\\\\|//)[^\r\n]*"),
)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact common credentials from structured or textual values."""
    if key is not None and _SENSITIVE_KEY.search(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(item_key): redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        result = value
        for pattern in _TOKEN_PATTERNS:
            result = pattern.sub(_REDACTED, result)
        return result
    return value


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    _standard_attributes = frozenset(logging.makeLogRecord({}).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        request_id = getattr(record, "request_id", None) or request_id_context.get()
        if request_id:
            request_id_text = str(request_id)
            redacted_request_id = redact(request_id_text)
            payload["request_id"] = (
                request_id_text
                if redacted_request_id == request_id_text
                and _SAFE_LOG_ID.fullmatch(request_id_text)
                else _REDACTED
            )
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        for name, value in record.__dict__.items():
            if name not in self._standard_attributes and name not in {"request_id", "message"}:
                payload[name] = redact(value, key=name)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    """Configure root logging without leaking token-like values."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
