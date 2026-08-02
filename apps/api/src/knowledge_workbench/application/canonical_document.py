from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class BoundingBox:
    left: float
    top: float
    right: float
    bottom: float
    origin: str
    page_width: float
    page_height: float
    unit: str = "pt"


@dataclass(frozen=True, slots=True)
class BlockProvenance:
    page_number: int | None = None
    bbox: BoundingBox | None = None
    source_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CanonicalBlock:
    block_id: str
    ordinal: int
    block_type: str
    text: str
    parent_block_id: str | None = None
    title: str | None = None
    heading_path: tuple[str, ...] = ()
    paragraph_index: int | None = None
    provenance: BlockProvenance = field(default_factory=BlockProvenance)

    @property
    def quote_hash(self) -> str:
        """SHA-256 of the exact, unnormalized UTF-8 text."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    blocks: tuple[CanonicalBlock, ...]
    page_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            asdict(self),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()
