from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from knowledge_workbench.application.ports.document_parser import EmptyDocumentError, ParseInput
from knowledge_workbench.infrastructure.parsing.docling import DoclingDocumentParser

FIXTURES = Path(__file__).parents[1] / "fixtures" / "d3_parser"


def _input(filename: str, media_type: str, *, ocr: bool = False) -> ParseInput:
    path = FIXTURES / filename
    return ParseInput(
        path=path,
        filename=filename,
        media_type=media_type,
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        max_pages=20,
        enable_ocr=ocr,
        ocr_languages=("en", "zh"),
    )


async def test_docling_parses_markdown_fixture_with_exact_quote_hashes() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    sample = next(item for item in manifest["samples"] if item["id"] == "markdown_nested_chinese")

    result = await DoclingDocumentParser().parse(
        _input("nested_chinese.md", "text/markdown")
    )

    assert result.parser_name == "docling"
    assert result.parser_version == "2.117.0"
    blocks_by_text = {block.text: block for block in result.document.blocks}
    for quote in sample["ordered_quotes"]:
        assert blocks_by_text[quote["text"]].quote_hash == quote["sha256_utf8"]
    assert [
        block.heading_path
        for block in result.document.blocks
        if block.text == "哈希必须稳定，引用必须可追溯。"
    ] == [("项目北极星", "采集阶段", "校验规则")]
    assert json.loads(result.native_json)
    assert "项目北极星" in result.markdown.decode()


@pytest.mark.skipif(
    os.getenv("RUN_DOCLING_PDF_TESTS") != "1",
    reason="requires locally cached Docling PDF models; set RUN_DOCLING_PDF_TESTS=1",
)
async def test_docling_parses_pdf_with_page_provenance() -> None:
    result = await DoclingDocumentParser().parse(
        _input("single_page_text.pdf", "application/pdf")
    )

    assert result.document.page_count == 1
    text_block = next(
        block
        for block in result.document.blocks
        if block.text == "This paragraph is known text on page one."
    )
    assert text_block.provenance.page_number == 1
    assert text_block.provenance.bbox is not None


async def test_docling_rejects_empty_fixture_without_initializing_models() -> None:
    with pytest.raises(EmptyDocumentError):
        await DoclingDocumentParser().parse(_input("empty.txt", "text/plain"))
