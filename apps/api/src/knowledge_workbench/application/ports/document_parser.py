from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from knowledge_workbench.application.canonical_document import CanonicalDocument


@dataclass(frozen=True, slots=True)
class ParseInput:
    path: Path
    filename: str
    media_type: str
    source_sha256: str
    max_pages: int
    enable_ocr: bool
    ocr_languages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParseResult:
    document: CanonicalDocument
    parser_name: str
    parser_version: str
    parser_config: dict[str, Any]
    native_json: bytes
    markdown: bytes
    warnings: tuple[dict[str, Any], ...] = ()


class DocumentParser(Protocol):
    async def parse(self, source: ParseInput) -> ParseResult: ...


class DocumentParseError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class UnsupportedDocumentError(DocumentParseError):
    def __init__(self, message: str = "The source format is not supported for parsing.") -> None:
        super().__init__("unsupported_document", message, retryable=False)


class EmptyDocumentError(DocumentParseError):
    def __init__(self, message: str = "The parser produced no usable text.") -> None:
        super().__init__("empty_document", message, retryable=False)


class OcrUnavailableError(DocumentParseError):
    def __init__(self, message: str = "OCR is required but unavailable.") -> None:
        super().__init__("ocr_unavailable", message, retryable=False)


class ParseResourceLimitError(DocumentParseError):
    def __init__(self, message: str = "The document exceeds parsing resource limits.") -> None:
        super().__init__("parse_resource_limit", message, retryable=False)


class ParseTimeoutError(DocumentParseError):
    def __init__(self, message: str = "Document parsing timed out.") -> None:
        super().__init__("parse_timeout", message, retryable=True)


class ParserDependencyError(DocumentParseError):
    def __init__(self, message: str = "The document parser is temporarily unavailable.") -> None:
        super().__init__("parser_dependency_unavailable", message, retryable=True)
