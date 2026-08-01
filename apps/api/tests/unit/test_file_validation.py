from __future__ import annotations

import hashlib

import pytest

from knowledge_workbench.application.file_validation import (
    inspect_content,
    validate_content_signature,
    validate_upload_declaration,
)
from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import SourceKind


async def _chunks(*values: bytes):
    for value in values:
        yield value


def test_validate_upload_declaration_accepts_supported_markdown() -> None:
    result = validate_upload_declaration(
        kind=SourceKind.MARKDOWN,
        original_filename="notes.md",
        media_type="text/markdown; charset=utf-8",
        size_bytes=12,
        content_sha256="a" * 64,
        max_size_bytes=25 * 1024 * 1024,
    )
    assert result == ("notes.md", "text/markdown")


@pytest.mark.parametrize(
    ("filename", "media_type", "size", "digest", "code"),
    [
        ("../notes.md", "text/markdown", 12, "a" * 64, "invalid_filename"),
        ("notes.txt", "text/markdown", 12, "a" * 64, "unsupported_file_extension"),
        ("notes.md", "text/plain", 12, "a" * 64, "unsupported_media_type"),
        ("notes.md", "text/markdown", 0, "a" * 64, "empty_upload"),
        ("notes.md", "text/markdown", 101, "a" * 64, "upload_too_large"),
        ("notes.md", "text/markdown", 12, "A" * 64, "invalid_content_sha256"),
    ],
)
def test_validate_upload_declaration_rejects_invalid_values(
    filename: str, media_type: str, size: int, digest: str, code: str
) -> None:
    with pytest.raises(AppError) as caught:
        validate_upload_declaration(
            kind=SourceKind.MARKDOWN,
            original_filename=filename,
            media_type=media_type,
            size_bytes=size,
            content_sha256=digest,
            max_size_bytes=100,
        )
    assert caught.value.code == code


async def test_inspect_content_hashes_all_chunks_and_validates_pdf() -> None:
    content = b"%PDF-1.7\nbody\n%%EOF\n"
    validation = await inspect_content(
        _chunks(content[:4], content[4:]),
        expected_size_bytes=len(content),
        max_size_bytes=100,
        validate_text=False,
    )
    assert validation.sha256 == hashlib.sha256(content).hexdigest()
    validate_content_signature(SourceKind.PDF, validation)


async def test_validate_content_signature_rejects_binary_text() -> None:
    content = b"hello\x00world"
    validation = await inspect_content(
        _chunks(content),
        expected_size_bytes=len(content),
        max_size_bytes=100,
        validate_text=True,
    )
    with pytest.raises(AppError) as caught:
        validate_content_signature(SourceKind.TEXT, validation)
    assert caught.value.code == "file_signature_mismatch"


async def test_inspect_content_validates_utf8_across_chunk_boundaries() -> None:
    content = "prefix € suffix".encode()
    validation = await inspect_content(
        _chunks(content[:8], content[8:9], content[9:]),
        expected_size_bytes=len(content),
        max_size_bytes=100,
        validate_text=True,
    )
    assert validation.text_valid_utf8 is True
    assert validation.text_has_nul is False


async def test_inspect_content_rejects_late_invalid_utf8() -> None:
    content = b"a" * (1024 * 1024 + 1) + b"\xff"
    validation = await inspect_content(
        _chunks(content[:1024 * 1024], content[1024 * 1024 :]),
        expected_size_bytes=len(content),
        max_size_bytes=len(content),
        validate_text=True,
    )
    assert validation.text_valid_utf8 is False


@pytest.mark.parametrize(
    ("content", "expected_size", "max_size", "code"),
    [
        (b"too long", 3, 100, "upload_size_mismatch"),
        (b"short", 10, 100, "upload_size_mismatch"),
        (b"too large", 9, 4, "upload_too_large"),
    ],
)
async def test_inspect_content_enforces_stream_size(
    content: bytes, expected_size: int, max_size: int, code: str
) -> None:
    with pytest.raises(AppError) as caught:
        await inspect_content(
            _chunks(content),
            expected_size_bytes=expected_size,
            max_size_bytes=max_size,
            validate_text=False,
        )
    assert caught.value.code == code


async def test_pdf_requires_eof_marker() -> None:
    content = b"%PDF-1.7\nbody"
    validation = await inspect_content(
        _chunks(content),
        expected_size_bytes=len(content),
        max_size_bytes=100,
        validate_text=False,
    )
    with pytest.raises(AppError) as caught:
        validate_content_signature(SourceKind.PDF, validation)
    assert caught.value.code == "file_signature_mismatch"
