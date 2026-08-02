from __future__ import annotations

import multiprocessing
import time
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from knowledge_workbench.application.ports.document_parser import (
    DocumentParseError,
    ParseInput,
    ParseTimeoutError,
)
from knowledge_workbench.infrastructure.parsing.process import (
    parse_docling_in_subprocess,
)


def _sleeping_target(connection: Connection, source: ParseInput) -> None:
    del source
    try:
        time.sleep(30)
    finally:
        connection.close()


def _parse_error_target(connection: Connection, source: ParseInput) -> None:
    del source
    connection.send(("parse_error", "bad_document", "cannot parse", False))
    connection.close()


def _input() -> ParseInput:
    return ParseInput(
        path=Path("fixture.txt"),
        filename="fixture.txt",
        media_type="text/plain",
        source_sha256="0" * 64,
        max_pages=10,
        enable_ocr=False,
    )


async def test_parse_timeout_terminates_spawned_process() -> None:
    started = time.monotonic()

    with pytest.raises(ParseTimeoutError):
        await parse_docling_in_subprocess(
            _input(),
            timeout_seconds=0.1,
            process_target=_sleeping_target,
        )

    assert time.monotonic() - started < 5
    assert all(
        child.name != "knowledge-workbench-docling"
        for child in multiprocessing.active_children()
    )


async def test_parse_process_preserves_parser_error_semantics() -> None:
    with pytest.raises(DocumentParseError) as caught:
        await parse_docling_in_subprocess(
            _input(),
            timeout_seconds=5,
            process_target=_parse_error_target,
        )

    assert caught.value.code == "bad_document"
    assert not caught.value.retryable
    assert str(caught.value) == "cannot parse"
