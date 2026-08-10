from __future__ import annotations

import hashlib
import math
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from knowledge_workbench.application.canonical_document import (
    BlockProvenance,
    CanonicalBlock,
    CanonicalDocument,
)
from knowledge_workbench.application.ports.document_parser import ParseInput
from knowledge_workbench.application.source_parsing import (
    SourceParsingService,
    _decode_cursor,
    _encode_cursor_position,
    section_from_block,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError
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


class _ScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _HistoricalSectionSession:
    def __init__(
        self,
        version: object,
        artifact: object,
        anchor: object,
        rows: list[object],
    ):
        self._scalar_values = iter([version, artifact, anchor])
        self.rows = rows

    async def scalar(self, _statement: object) -> object:
        return next(self._scalar_values)

    async def scalars(self, _statement: object) -> _ScalarRows:
        return _ScalarRows(self.rows)


async def test_historical_artifact_anchor_is_read_without_current_redirect() -> None:
    space_id = uuid4()
    source_id = uuid4()
    version_id = uuid4()
    current_artifact_id = uuid4()
    historical_artifact_id = uuid4()
    section_id = uuid4()
    version = SimpleNamespace(
        id=version_id,
        current_parse_artifact_id=current_artifact_id,
    )
    artifact = SimpleNamespace(id=historical_artifact_id, revision=1)
    anchor = SimpleNamespace(id=section_id, ordinal=18)
    section = SimpleNamespace(
        id=section_id,
        ordinal=18,
        parse_artifact_id=historical_artifact_id,
    )
    session = _HistoricalSectionSession(
        version,
        artifact,
        anchor,
        [section],
    )

    page = await SourceParsingService(Settings(app_env="test")).list_sections(  # type: ignore[arg-type]
        session,
        space_id=space_id,
        source_id=source_id,
        version_id=version_id,
        limit=20,
        cursor=None,
        artifact_id=historical_artifact_id,
        anchor_section_id=section_id,
    )

    assert page.artifact.id == historical_artifact_id
    assert page.artifact.id != current_artifact_id
    assert page.items == [section]
    assert page.previous_cursor is not None
    _, cursor_ordinal, _, before_ordinal = _decode_cursor(page.previous_cursor)
    assert cursor_ordinal == -1
    assert before_ordinal == 8


async def test_previous_cursor_returns_non_overlapping_window_and_can_continue() -> None:
    artifact_id = uuid4()
    version = SimpleNamespace(id=uuid4(), current_parse_artifact_id=artifact_id)
    artifact = SimpleNamespace(id=artifact_id, revision=1)
    earlier = [
        SimpleNamespace(id=uuid4(), ordinal=ordinal, parse_artifact_id=artifact_id)
        for ordinal in range(28, 48)
    ]
    session = _HistoricalSectionSession(version, artifact, None, earlier)

    page = await SourceParsingService(Settings(app_env="test")).list_sections(  # type: ignore[arg-type]
        session,
        space_id=uuid4(),
        source_id=uuid4(),
        version_id=version.id,
        limit=20,
        cursor=_encode_cursor_position(
            artifact_id,
            27,
            uuid4(),
            before_ordinal=48,
        ),
        artifact_id=artifact_id,
        anchor_section_id=None,
    )

    assert [item.ordinal for item in page.items] == list(range(28, 48))
    assert page.next_cursor is None
    assert page.previous_cursor is not None
    _, cursor_ordinal, _, before_ordinal = _decode_cursor(page.previous_cursor)
    assert cursor_ordinal == 7
    assert before_ordinal == 28


async def test_historical_anchor_must_belong_to_selected_artifact() -> None:
    version = SimpleNamespace(id=uuid4(), current_parse_artifact_id=uuid4())
    artifact = SimpleNamespace(id=uuid4(), revision=1)
    session = _HistoricalSectionSession(version, artifact, None, [])

    with pytest.raises(AppError) as caught:
        await SourceParsingService(Settings(app_env="test")).list_sections(  # type: ignore[arg-type]
            session,
            space_id=uuid4(),
            source_id=uuid4(),
            version_id=version.id,
            limit=20,
            cursor=None,
            artifact_id=artifact.id,
            anchor_section_id=uuid4(),
        )

    assert caught.value.code == "source_section_not_found"
