from __future__ import annotations

from uuid import uuid4

import pytest

from knowledge_workbench.application.chunking import TextChunkInput, chunk_inputs


def _input(text: str) -> TextChunkInput:
    return TextChunkInput(
        target_id=uuid4(),
        section_id=uuid4(),
        text=text,
        heading_path=["Heading"],
        locator={"page": 1},
    )


def test_chunking_handles_chinese_without_spaces_and_overlap() -> None:
    drafts = chunk_inputs(
        [_input("甲乙丙丁戊己庚辛壬癸")],
        target_characters=6,
        overlap_characters=2,
    )

    assert [draft.text for draft in drafts] == ["甲乙丙丁戊己", "戊己庚辛壬癸"]
    assert drafts[0].locator == {"page": 1, "char_start": 0, "char_end": 6}
    assert drafts[1].locator == {"page": 1, "char_start": 4, "char_end": 10}


def test_chunk_identity_changes_with_versions() -> None:
    item = _input("stable content")
    first = chunk_inputs(
        [item],
        target_characters=100,
        overlap_characters=0,
        chunker_version="v1",
        index_config_version="i1",
    )[0]
    second = chunk_inputs(
        [item],
        target_characters=100,
        overlap_characters=0,
        chunker_version="v2",
        index_config_version="i1",
    )[0]
    third = chunk_inputs(
        [item],
        target_characters=100,
        overlap_characters=0,
        chunker_version="v1",
        index_config_version="i2",
    )[0]

    assert len({first.content_identity, second.content_identity, third.content_identity}) == 3
    assert first.content_hash == second.content_hash == third.content_hash


@pytest.mark.parametrize(
    ("target", "overlap"),
    [(0, 0), (10, -1), (10, 10), (10, 11)],
)
def test_chunking_rejects_invalid_sizes(target: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_inputs([_input("text")], target_characters=target, overlap_characters=overlap)
