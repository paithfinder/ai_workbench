"""Credential redaction unit tests."""

import json
import logging

from ai_workbench_api.logging import JsonFormatter, redact


def test_redact_common_provider_tokens_and_sensitive_fields() -> None:
    payload = {
        "openai": "sk-proj-AbCdEfGhIjKlMnOp",
        "github": "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "notion_legacy": "secret_abcdefghijklmnopqrstuvwxyz123456",
        "notion_current": "ntn_abcdefghijklmnopqrstuvwxyz123456",
        "authorization": "Bearer visible-value",
        "nested": ["safe", "Bearer abcdefghijklmnop"],
    }

    result = redact(payload)

    assert result["openai"] == "[REDACTED]"
    assert result["github"] == "[REDACTED]"
    assert result["notion_legacy"] == "[REDACTED]"
    assert result["notion_current"] == "[REDACTED]"
    assert result["authorization"] == "[REDACTED]"
    assert result["nested"] == ["safe", "[REDACTED]"]


def test_json_formatter_redacts_message_and_extra() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token ghp_abcdefghijklmnopqrstuvwxyz123456",
        args=(),
        exc_info=None,
    )
    record.api_key = "sk-proj-AbCdEfGhIjKlMnOp"

    output = json.loads(JsonFormatter().format(record))

    assert output["message"] == "token [REDACTED]"
    assert output["api_key"] == "[REDACTED]"
