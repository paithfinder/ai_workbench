from __future__ import annotations

import codecs
import hashlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import PurePath

from knowledge_workbench.core.errors import AppError
from knowledge_workbench.db.models import SourceKind

MAX_TEXT_CONTROL_RATIO = 0.02
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class FileRule:
    kind: SourceKind
    extension: str
    media_type: str


FILE_RULES = {
    SourceKind.PDF: FileRule(SourceKind.PDF, ".pdf", "application/pdf"),
    SourceKind.MARKDOWN: FileRule(SourceKind.MARKDOWN, ".md", "text/markdown"),
    SourceKind.TEXT: FileRule(SourceKind.TEXT, ".txt", "text/plain"),
}


@dataclass(frozen=True, slots=True)
class ContentValidation:
    sha256: str
    size_bytes: int
    prefix: bytes
    suffix: bytes
    text_characters: int
    text_controls: int
    text_has_nul: bool
    text_valid_utf8: bool


def validate_upload_declaration(
    *,
    kind: SourceKind,
    original_filename: str,
    media_type: str,
    size_bytes: int,
    content_sha256: str,
    max_size_bytes: int,
) -> tuple[str, str]:
    filename = _safe_filename(original_filename)
    rule = FILE_RULES.get(kind)
    if rule is None:
        raise AppError(
            "unsupported_source_kind",
            "Only PDF, Markdown, and TXT files are supported.",
            status_code=422,
        )
    if PurePath(filename).suffix.lower() != rule.extension:
        raise AppError(
            "unsupported_file_extension",
            f"This source requires a {rule.extension} file.",
            status_code=422,
        )
    normalized_media_type = media_type.split(";", 1)[0].strip().lower()
    if normalized_media_type != rule.media_type:
        raise AppError(
            "unsupported_media_type",
            f"The file must use media type {rule.media_type}.",
            status_code=422,
        )
    if size_bytes <= 0:
        raise AppError("empty_upload", "The selected file is empty.", status_code=422)
    if size_bytes > max_size_bytes:
        raise AppError(
            "upload_too_large",
            f"The selected file exceeds the {max_size_bytes // (1024 * 1024)} MiB limit.",
            status_code=413,
        )
    if SHA256_PATTERN.fullmatch(content_sha256) is None:
        raise AppError(
            "invalid_content_sha256",
            "content_sha256 must be a lowercase SHA-256 digest.",
            status_code=422,
        )
    return filename, normalized_media_type


async def inspect_content(
    chunks: AsyncIterator[bytes],
    *,
    expected_size_bytes: int,
    max_size_bytes: int,
    validate_text: bool,
) -> ContentValidation:
    digest = hashlib.sha256()
    prefix = bytearray()
    suffix = bytearray()
    size_bytes = 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict") if validate_text else None
    text_characters = 0
    text_controls = 0
    text_has_nul = False
    text_valid_utf8 = True

    async for chunk in chunks:
        size_bytes += len(chunk)
        if size_bytes > max_size_bytes:
            raise AppError(
                "upload_too_large",
                "The uploaded object exceeds the configured upload limit.",
                status_code=413,
            )
        if size_bytes > expected_size_bytes:
            raise AppError(
                "upload_size_mismatch",
                "The uploaded object contains more bytes than reserved.",
                status_code=422,
            )
        digest.update(chunk)
        if len(prefix) < 8:
            prefix.extend(chunk[: 8 - len(prefix)])
        suffix.extend(chunk)
        if len(suffix) > 1024:
            del suffix[:-1024]
        if decoder is not None:
            try:
                decoded = decoder.decode(chunk)
            except UnicodeDecodeError:
                text_valid_utf8 = False
                decoded = ""
            text_characters += len(decoded)
            text_has_nul = text_has_nul or "\x00" in decoded
            text_controls += sum(
                1 for character in decoded if ord(character) < 32 and character not in "\n\r\t"
            )
    if size_bytes != expected_size_bytes:
        raise AppError(
            "upload_size_mismatch",
            "The uploaded object size does not match the reserved file.",
            status_code=422,
        )
    if decoder is not None and text_valid_utf8:
        try:
            decoded = decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            text_valid_utf8 = False
        else:
            text_characters += len(decoded)
            text_has_nul = text_has_nul or "\x00" in decoded
            text_controls += sum(
                1 for character in decoded if ord(character) < 32 and character not in "\n\r\t"
            )
    return ContentValidation(
        digest.hexdigest(),
        size_bytes,
        bytes(prefix),
        bytes(suffix),
        text_characters,
        text_controls,
        text_has_nul,
        text_valid_utf8,
    )


def validate_content_signature(kind: SourceKind, validation: ContentValidation) -> None:
    if kind == SourceKind.PDF:
        if not validation.prefix.startswith(b"%PDF-") or b"%%EOF" not in validation.suffix:
            raise AppError(
                "file_signature_mismatch",
                "The uploaded PDF must have a PDF header and end-of-file marker.",
                status_code=422,
            )
        return

    if not validation.text_valid_utf8:
        raise AppError(
            "file_signature_mismatch",
            "Markdown and TXT files must use UTF-8 text encoding.",
            status_code=422,
        )
    if validation.text_has_nul:
        raise AppError(
            "file_signature_mismatch",
            "The uploaded text file contains binary content.",
            status_code=422,
        )
    if (
        validation.text_characters
        and validation.text_controls / validation.text_characters > MAX_TEXT_CONTROL_RATIO
    ):
        raise AppError(
            "file_signature_mismatch",
            "The uploaded text file contains too much binary control data.",
            status_code=422,
        )


def _safe_filename(value: str) -> str:
    filename = value.strip()
    if (
        not filename
        or filename != PurePath(filename).name
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise AppError("invalid_filename", "The file name is not valid.", status_code=422)
    if len(filename) > 500:
        raise AppError("invalid_filename", "The file name is too long.", status_code=422)
    return filename
