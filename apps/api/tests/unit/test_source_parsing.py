from __future__ import annotations

import hashlib
import math
from pathlib import Path
from uuid import uuid4

import pytest

from knowledge_workbench.application.canonical_document import (
    BlockProvenance,
    CanonicalBlock,
    CanonicalDocument,
)
from knowledge_workbench.application.ports.document_parser import ParseInput
from knowledge_workbench.application.source_parsing import section_from_block
from knowledge_workbench.infrastructure.parsing.fake import DeterministicFakeParser


def test_quote_hash_uses_exact_raw_utf8_text() -> None:
    first = CanonicalBlock(
        block_id="a", ordinal=0, block_type="paragraph", text=" 文本\n"
    )
    second = CanonicalBlock(
        block_id="b", ordinal=1, block_type="paragraph", text="文本"
    )

    assert first.quote_hash == hashlib.sha256(" 文本\n".encode()).hexdigest()
    assert first.quote_hash != second.quote_hash


def test_canonical_json_and_content_hash_are_deterministic() -> None:
    document = CanonicalDocument(
        blocks=(
            CanonicalBlock(
                block_id="block-1",
                ordinal=0,
                block_type="paragraph",
                text="é",
            ),
        )
    )

    assert document.to_json_bytes() == document.to_json_bytes()
    assert document.content_sha256 == hashlib.sha256(document.to_json_bytes()).hexdigest()


@pytest.mark.parametrize("non_finite", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(non_finite: float) -> None:
    document = CanonicalDocument(
        blocks=(
            CanonicalBlock(
                block_id="block-1",
                ordinal=0,
                block_type="paragraph",
                text="text",
                provenance=BlockProvenance(metadata={"score": non_finite}),
            ),
        )
    )

    with pytest.raises(ValueError, match="Out of range float values"):
        document.to_json_bytes()


async def test_fake_parser_preserves_markdown_order_and_heading_path(tmp_path: Path) -> None:
    source_path = tmp_path / "notes.md"
    source_path.write_text("# 第一章\n第一段\n## 小节\n第二段\n", encoding="utf-8")

    result = await DeterministicFakeParser().parse(
        ParseInput(
            path=source_path,
            filename="notes.md",
            media_type="text/markdown",
            source_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
            max_pages=20,
            enable_ocr=False,
        )
    )

    assert [block.ordinal for block in result.document.blocks] == [0, 1, 2, 3]
    assert result.document.blocks[-1].heading_path == ("第一章", "小节")
    assert result.parser_name == "deterministic-fake"


def test_section_locator_freezes_source_version_artifact_and_section() -> None:
    source_id = uuid4()
    version_id = uuid4()
    artifact_id = uuid4()
    section_id = uuid4()
    block = CanonicalBlock(
        block_id="paragraph-1",
        ordinal=3,
        block_type="paragraph",
        text=" exact quote ",
        heading_path=("Chapter",),
        paragraph_index=2,
        provenance=BlockProvenance(page_number=1),
    )

    section = section_from_block(
        block=block,
        section_id=section_id,
        space_id=uuid4(),
        source_id=source_id,
        version_id=version_id,
        artifact_id=artifact_id,
        revision=4,
        content_hash="a" * 64,
    )

    assert section.quote_hash == hashlib.sha256(b" exact quote ").hexdigest()
    assert section.locator == {
        "sourceId": str(source_id),
        "sourceVersionId": str(version_id),
        "parseArtifactId": str(artifact_id),
        "parseRevision": 4,
        "sectionId": str(section_id),
        "locatorType": "page_heading_paragraph",
        "page": 1,
        "headingPath": ["Chapter"],
        "paragraphIndex": 2,
        "bbox": None,
        "quoteHash": section.quote_hash,
    }
