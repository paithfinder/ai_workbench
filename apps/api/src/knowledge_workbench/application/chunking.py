from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from knowledge_workbench.db.models import SourceSection


@dataclass(frozen=True, slots=True)
class TextChunkInput:
    target_id: UUID
    section_id: UUID
    text: str
    heading_path: list[str]
    locator: dict[str, object]


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    ordinal: int
    text: str
    content_hash: str
    content_identity: str
    section_id: UUID
    heading_path: list[str]
    locator: dict[str, object]


def content_identity(
    *,
    target_id: UUID,
    section_id: UUID,
    ordinal: int,
    content_hash: str,
    chunker_version: str,
    index_config_version: str,
) -> str:
    return hashlib.sha256(
        f"{target_id}:{section_id}:{ordinal}:{content_hash}:{chunker_version}:"
        f"{index_config_version}".encode()
    ).hexdigest()


def chunk_inputs(
    inputs: Iterable[TextChunkInput],
    *,
    target_characters: int,
    overlap_characters: int,
    chunker_version: str = "d7-structured-v1",
    index_config_version: str = "d7-v1",
) -> list[ChunkDraft]:
    if target_characters <= 0 or overlap_characters < 0 or overlap_characters >= target_characters:
        raise ValueError("Chunk target must be positive and overlap must be smaller than target")
    drafts: list[ChunkDraft] = []
    for item in inputs:
        normalized = re.sub(r"\s+", " ", item.text).strip()
        if not normalized:
            continue
        start = 0
        while start < len(normalized):
            end = min(len(normalized), start + target_characters)
            if end < len(normalized):
                boundary = normalized.rfind(" ", start, end)
                if boundary > start + target_characters // 2:
                    end = boundary
            chunk_text = normalized[start:end].strip()
            if chunk_text:
                digest = hashlib.sha256(chunk_text.encode()).hexdigest()
                ordinal = len(drafts)
                drafts.append(
                    ChunkDraft(
                        ordinal=ordinal,
                        text=chunk_text,
                        content_hash=digest,
                        content_identity=content_identity(
                            target_id=item.target_id,
                            section_id=item.section_id,
                            ordinal=ordinal,
                            content_hash=digest,
                            chunker_version=chunker_version,
                            index_config_version=index_config_version,
                        ),
                        section_id=item.section_id,
                        heading_path=item.heading_path,
                        locator={**item.locator, "char_start": start, "char_end": end},
                    )
                )
            if end >= len(normalized):
                break
            start = max(start + 1, end - overlap_characters)
    return drafts


def chunk_sections(
    sections: Iterable[SourceSection],
    *,
    target_id: UUID | None = None,
    target_characters: int,
    overlap_characters: int,
    chunker_version: str = "d7-structured-v1",
    index_config_version: str = "d7-v1",
) -> list[ChunkDraft]:
    return chunk_inputs(
        (
            TextChunkInput(
                target_id=target_id or section.source_version_id,
                section_id=section.id,
                text=section.text,
                heading_path=list(section.heading_path),
                locator=dict(section.locator),
            )
            for section in sections
        ),
        target_characters=target_characters,
        overlap_characters=overlap_characters,
        chunker_version=chunker_version,
        index_config_version=index_config_version,
    )
