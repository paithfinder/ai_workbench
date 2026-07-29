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


def test_redact_paths_preview_tokens_bodies_and_private_keys() -> None:
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "not-real-secret-material\n"
        "-----END PRIVATE KEY-----"
    )
    payload = {
        "message": r"scan failed for D:\workbench\private\source.py",
        "unc": r"location \\server\share\source.py unavailable",
        "preview_token": "opaque-preview-value",
        "request_body": {"path": r"D:\private"},
        "exception": f"bad key {private_key}",
    }

    result = redact(payload)

    redacted = "[REDACTED]"
    assert result["message"] == f"scan failed for {redacted}"
    assert result["unc"] == f"location {redacted}"
    assert result["preview_token"] == redacted
    assert result["request_body"] == redacted
    assert result["exception"] == f"bad key {redacted}"
    assert "workbench" not in json.dumps(result)
    assert "not-real-secret-material" not in json.dumps(result)


def test_redact_unquoted_windows_and_unc_paths_with_spaces_without_suffix_leak() -> None:
    messages = [
        r"failed D:\Users\Jane Doe\private\source.py",
        r"failed \\server\team share\private\source.py",
        r"failed D:\Users\Jane Doe\extensionless source",
        r"failed \\server\team share\extensionless source",
    ]

    results = [redact(message) for message in messages]

    assert results == ["failed [REDACTED]"] * 4
    assert all("Doe" not in result and "source.py" not in result for result in results)


def test_json_formatter_defensively_redacts_invalid_request_id() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="safe",
        args=(),
        exc_info=None,
    )
    record.request_id = r"unsafe D:\Users\Jane Doe\request.txt"

    output = json.loads(JsonFormatter().format(record))

    assert output["request_id"] == "[REDACTED]"


def test_json_formatter_redacts_token_shaped_request_id() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="safe",
        args=(),
        exc_info=None,
    )
    record.request_id = "ghp_abcdefghijklmnopqrstuvwxyz123456"

    output = json.loads(JsonFormatter().format(record))

    assert output["request_id"] == "[REDACTED]"
