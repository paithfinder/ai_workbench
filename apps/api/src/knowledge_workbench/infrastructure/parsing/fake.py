from __future__ import annotations

import json
import re
from pathlib import Path

from knowledge_workbench.application.canonical_document import (
    BlockProvenance,
    CanonicalBlock,
    CanonicalDocument,
)
from knowledge_workbench.application.ports.document_parser import ParseInput, ParseResult

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


class DeterministicFakeParser:
    """Deterministic parser for tests; never wired into production settings."""

    async def parse(self, source: ParseInput) -> ParseResult:
        text = source.path.read_text(encoding="utf-8")
        blocks: list[CanonicalBlock] = []
        heading_path: list[str] = []
        paragraph_index = 0
        for raw in text.splitlines():
            if not raw:
                continue
            heading = _HEADING.match(raw) if source.media_type == "text/markdown" else None
            if heading:
                level = len(heading.group(1))
                title = heading.group(2)
                heading_path[level - 1 :] = [title]
                block_type = "heading"
                block_text = title
                block_title: str | None = title
                paragraph = None
            else:
                block_type = "paragraph"
                block_text = raw
                block_title = None
                paragraph = paragraph_index
                paragraph_index += 1
            ordinal = len(blocks)
            blocks.append(
                CanonicalBlock(
                    block_id=f"block-{ordinal:06d}",
                    ordinal=ordinal,
                    block_type=block_type,
                    text=block_text,
                    title=block_title,
                    heading_path=tuple(heading_path),
                    paragraph_index=paragraph,
                    provenance=BlockProvenance(source_ref=f"line:{ordinal + 1}"),
                )
            )
        document = CanonicalDocument(blocks=tuple(blocks), page_count=None)
        native = json.dumps(
            {"text": text, "format": source.media_type},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return ParseResult(
            document=document,
            parser_name="deterministic-fake",
            parser_version="1",
            parser_config={"ocr": False},
            native_json=native,
            markdown=text.encode("utf-8"),
        )


def write_fake_source(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
