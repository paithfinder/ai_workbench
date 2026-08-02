from __future__ import annotations

import asyncio
import multiprocessing
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal, cast

from knowledge_workbench.application.ports.document_parser import (
    DocumentParseError,
    ParseInput,
    ParserDependencyError,
    ParseResult,
    ParseTimeoutError,
)

type _ProcessMessage = (
    tuple[Literal["ok"], ParseResult]
    | tuple[Literal["parse_error"], str, str, bool]
    | tuple[Literal["crash"], str]
)
_ProcessTarget = Callable[[Connection, ParseInput], None]


async def parse_docling_in_subprocess(
    source: ParseInput,
    *,
    timeout_seconds: float,
    process_target: _ProcessTarget | None = None,
) -> ParseResult:
    """Run Docling in a disposable spawned process with a hard deadline."""

    if timeout_seconds <= 0:
        raise ValueError("parser timeout must be positive")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=process_target or _docling_process_main,
        args=(sender, source),
        name="knowledge-workbench-docling",
        daemon=True,
    )
    process.start()
    sender.close()
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            if receiver.poll():
                message: _ProcessMessage = receiver.recv()
                await asyncio.to_thread(process.join, 1.0)
                return _unwrap_message(message)
            if not process.is_alive():
                await asyncio.to_thread(process.join)
                if receiver.poll():
                    message = receiver.recv()
                    return _unwrap_message(message)
                raise ParserDependencyError(
                    "The document parser process exited without a result."
                )
            await asyncio.sleep(0.05)
        raise ParseTimeoutError()
    finally:
        receiver.close()
        await _terminate_process(process)


async def _terminate_process(process: BaseProcess) -> None:
    if not process.is_alive():
        await asyncio.to_thread(process.join)
        return
    process.terminate()
    await asyncio.to_thread(process.join, 1.0)
    if process.is_alive():
        process.kill()
        await asyncio.to_thread(process.join)


def _docling_process_main(connection: Connection, source: ParseInput) -> None:
    try:
        from knowledge_workbench.infrastructure.parsing.docling import (
            DoclingDocumentParser,
        )

        result = asyncio.run(DoclingDocumentParser().parse(source))
        connection.send(("ok", result))
    except DocumentParseError as exc:
        connection.send(("parse_error", exc.code, str(exc), exc.retryable))
    except BaseException as exc:
        connection.send(("crash", type(exc).__name__))
    finally:
        connection.close()


def _unwrap_message(message: _ProcessMessage) -> ParseResult:
    kind = message[0]
    if kind == "ok":
        return cast(ParseResult, message[1])
    if kind == "parse_error":
        parse_error = cast(tuple[Literal["parse_error"], str, str, bool], message)
        raise DocumentParseError(
            parse_error[1],
            parse_error[2],
            retryable=parse_error[3],
        )
    crash = cast(tuple[Literal["crash"], str], message)
    raise ParserDependencyError(
        f"The document parser process failed ({crash[1]})."
    )
