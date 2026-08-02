from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from knowledge_workbench.application.canonical_document import (
    BlockProvenance,
    BoundingBox,
    CanonicalBlock,
    CanonicalDocument,
)
from knowledge_workbench.application.ports.document_parser import (
    EmptyDocumentError,
    ParseInput,
    ParserDependencyError,
    ParseResourceLimitError,
    ParseResult,
    UnsupportedDocumentError,
)


class DoclingDocumentParser:
    """Docling 2.117 adapter. Heavy imports and model setup occur only on parse workers."""

    async def parse(self, source: ParseInput) -> ParseResult:
        if source.path.stat().st_size == 0:
            raise EmptyDocumentError()
        try:
            result = await asyncio.to_thread(self._convert, source)
        except (EmptyDocumentError, ParseResourceLimitError, UnsupportedDocumentError):
            raise
        except Exception as exc:
            raise ParserDependencyError(str(exc)) from exc
        return result

    @staticmethod
    def _convert(source: ParseInput) -> ParseResult:
        if source.media_type == "text/plain" or source.path.suffix.lower() == ".txt":
            return _convert_plain_text(source)
        converter = _get_converter(source.enable_ocr, source.ocr_languages)
        try:
            conversion = converter.convert(
                source.path,
                raises_on_error=True,
                max_num_pages=source.max_pages,
                max_file_size=source.path.stat().st_size,
            )
        except RuntimeError as exc:
            message = str(exc)
            if "max_num_pages" in message or (
                "page" in message.lower() and "limit" in message.lower()
            ):
                raise ParseResourceLimitError(message) from exc
            if "not valid" in message.lower() or "conversion failed" in message.lower():
                raise UnsupportedDocumentError(message) from exc
            raise
        except ValueError as exc:
            message = str(exc)
            if "format" in message.lower() or "extension" in message.lower():
                raise UnsupportedDocumentError(message) from exc
            raise
        document = conversion.document
        canonical = _canonicalize(document)
        if not canonical.blocks or not any(block.text for block in canonical.blocks):
            raise EmptyDocumentError()
        native = json.dumps(
            document.export_to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        markdown = document.export_to_markdown().encode("utf-8")
        return ParseResult(
            document=canonical,
            parser_name="docling",
            parser_version=_docling_version(),
            parser_config={
                "do_ocr": source.enable_ocr,
                "ocr_languages": list(source.ocr_languages),
                "max_pages": source.max_pages,
            },
            native_json=native,
            markdown=markdown,
        )


def _convert_plain_text(source: ParseInput) -> ParseResult:
    try:
        text = Path(source.path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedDocumentError("Plain-text sources must be valid UTF-8.") from exc
    blocks = tuple(
        CanonicalBlock(
            block_id=f"text:{index}",
            ordinal=index,
            block_type="paragraph",
            text=paragraph,
            paragraph_index=index,
            provenance=BlockProvenance(source_ref=f"line-group:{index}"),
        )
        for index, paragraph in enumerate(part for part in text.splitlines() if part)
    )
    if not blocks:
        raise EmptyDocumentError()
    document = CanonicalDocument(blocks=blocks)
    native = json.dumps(
        {"format": "plain_text", "text": text},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ParseResult(
        document=document,
        parser_name="deterministic-text",
        parser_version="1",
        parser_config={"encoding": "utf-8"},
        native_json=native,
        markdown=text.encode("utf-8"),
    )


@lru_cache(maxsize=8)
def _get_converter(enable_ocr: bool, ocr_languages: tuple[str, ...]) -> Any:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pdf_options = PdfPipelineOptions(do_ocr=enable_ocr, do_table_structure=True)
    if ocr_languages and hasattr(pdf_options, "ocr_options"):
        pdf_options.ocr_options.lang = list(ocr_languages)
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.HTML, InputFormat.MD],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)},
    )


def _canonicalize(document: Any) -> CanonicalDocument:
    from docling_core.types.doc.items.node import DocItem
    from docling_core.types.doc.items.table.table import TableItem
    from docling_core.types.doc.items.text import SectionHeaderItem, TextItem, TitleItem

    document_title: str | None = None
    headings: list[str] = []
    blocks: list[CanonicalBlock] = []
    paragraph_index = 0
    for item, _tree_depth in document.iterate_items(with_groups=False):
        if isinstance(item, TableItem):
            table_text = item.export_to_markdown(document).strip()
            if not table_text:
                continue
            provenance = (
                _provenance(document, item.prov[0]) if item.prov else None
            )
            heading_path = tuple(
                [document_title, *headings] if document_title is not None else headings
            )
            blocks.append(
                CanonicalBlock(
                    block_id=str(item.self_ref),
                    parent_block_id=item.parent.cref if item.parent else None,
                    ordinal=len(blocks),
                    block_type="table",
                    text=table_text,
                    heading_path=heading_path,
                    paragraph_index=paragraph_index,
                    provenance=provenance
                    or BlockProvenance(source_ref=str(item.self_ref)),
                )
            )
            paragraph_index += 1
            continue
        if not isinstance(item, TextItem):
            continue
        raw_text = item.orig if item.orig is not None else item.text
        if not raw_text:
            continue
        title: str | None = None
        if isinstance(item, TitleItem):
            document_title = item.text
            headings.clear()
            block_type = "title"
            title = item.text
            paragraph: int | None = None
        elif isinstance(item, SectionHeaderItem):
            level = max(1, item.level)
            headings[level - 1 :] = [item.text]
            block_type = "heading"
            title = item.text
            paragraph = None
        else:
            block_type = str(item.label.value if hasattr(item.label, "value") else item.label)
            paragraph = paragraph_index
            paragraph_index += 1
        heading_path = tuple(
            [document_title, *headings] if document_title is not None else headings
        )
        provenance = (
            _provenance(document, item.prov[0])
            if isinstance(item, DocItem) and item.prov
            else None
        )
        blocks.append(
            CanonicalBlock(
                block_id=str(item.self_ref),
                parent_block_id=item.parent.cref if item.parent else None,
                ordinal=len(blocks),
                block_type=block_type,
                title=title,
                text=raw_text,
                heading_path=heading_path,
                paragraph_index=paragraph,
                provenance=provenance or BlockProvenance(source_ref=str(item.self_ref)),
            )
        )
    page_count = len(getattr(document, "pages", {}) or {}) or None
    return CanonicalDocument(blocks=tuple(blocks), page_count=page_count)


def _provenance(document: Any, value: Any) -> BlockProvenance:
    bbox = value.bbox
    page = value.page_no
    page_model = (getattr(document, "pages", {}) or {}).get(page)
    size = getattr(page_model, "size", None)
    origin = getattr(bbox, "coord_origin", "bottom_left")
    origin_value = origin.value if hasattr(origin, "value") else str(origin)
    return BlockProvenance(
        page_number=page,
        bbox=BoundingBox(
            left=float(bbox.l),
            top=float(bbox.t),
            right=float(bbox.r),
            bottom=float(bbox.b),
            origin=origin_value,
            page_width=float(getattr(size, "width", 0.0)),
            page_height=float(getattr(size, "height", 0.0)),
            unit="pt",
        ),
        metadata={"charspan": list(value.charspan)},
    )


def _docling_version() -> str:
    try:
        return version("docling")
    except PackageNotFoundError:
        return "2.117.0"
