from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "d3_parser"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
EXPECTED_SAMPLE_FILES = {
    "empty.txt",
    "image_only.pdf",
    "malformed.pdf",
    "multipage_text.pdf",
    "nested_chinese.md",
    "ordered_unicode.txt",
    "simple_table.pdf",
    "single_page_text.pdf",
    "static_main_table.html",
}
OUTCOMES = {"success", "no_extractable_text", "failure_empty", "failure_malformed"}
FORMAT_EXTENSIONS = {
    "html": ".html",
    "markdown": ".md",
    "pdf": ".pdf",
    "text": ".txt",
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_d3_fixture_manifest_is_complete_and_hashes_are_current() -> None:
    manifest = _load_manifest()

    assert manifest["schema_version"] == 1
    assert manifest["quote_hash_contract"] == (
        "lowercase SHA-256 of exact, unnormalized UTF-8 quote bytes"
    )
    assert manifest["page_numbering"] == "1-based"
    assert manifest["provenance"]["origin"]
    assert manifest["provenance"]["license_safety"]

    samples = manifest["samples"]
    assert 5 <= len(samples) <= 10

    ids = [sample["id"] for sample in samples]
    filenames = [sample["filename"] for sample in samples]
    assert len(ids) == len(set(ids))
    assert len(filenames) == len(set(filenames))
    assert set(filenames) == EXPECTED_SAMPLE_FILES
    assert {path.name for path in FIXTURE_DIR.iterdir() if path.name != MANIFEST_PATH.name} == (
        EXPECTED_SAMPLE_FILES
    )

    for sample in samples:
        fixture_path = FIXTURE_DIR / sample["filename"]
        payload = fixture_path.read_bytes()

        assert fixture_path.is_file()
        assert fixture_path.parent == FIXTURE_DIR
        assert sample["size_bytes"] == len(payload)
        assert SHA256_PATTERN.fullmatch(sample["sha256"])
        assert sample["sha256"] == _sha256(payload)
        assert sample["expected_outcome"] in OUTCOMES
        assert sample["format"] in FORMAT_EXTENSIONS
        assert fixture_path.suffix == FORMAT_EXTENSIONS[sample["format"]]
        assert isinstance(sample["valid_format"], bool)
        assert isinstance(sample["headings"], list)
        assert isinstance(sample["ordered_quotes"], list)
        assert sample["notes"]

        page_count = sample["page_count"]
        assert page_count is None or isinstance(page_count, int) and page_count >= 1

        for heading in sample["headings"]:
            assert isinstance(heading["level"], int) and heading["level"] >= 1
            assert heading["text"]
            if "page" in heading:
                assert page_count is not None
                assert 1 <= heading["page"] <= page_count

        for quote in sample["ordered_quotes"]:
            text = quote["text"]
            assert isinstance(text, str) and text
            assert SHA256_PATTERN.fullmatch(quote["sha256_utf8"])
            assert quote["sha256_utf8"] == _sha256(text.encode("utf-8"))
            if "page" in quote:
                assert page_count is not None
                assert 1 <= quote["page"] <= page_count


def test_d3_text_fixtures_are_utf8_and_control_free() -> None:
    for filename in ("nested_chinese.md", "ordered_unicode.txt", "static_main_table.html"):
        payload = (FIXTURE_DIR / filename).read_bytes()
        assert not payload.startswith(b"\xef\xbb\xbf")
        text = payload.decode("utf-8")
        assert not any(
            ord(character) < 32 and character not in "\n\r\t" for character in text
        )
        assert not any(127 <= ord(character) <= 159 for character in text)


def test_d3_pdf_fixture_structure_matches_declared_validity() -> None:
    manifest = _load_manifest()

    for sample in manifest["samples"]:
        if sample["format"] != "pdf":
            continue

        payload = (FIXTURE_DIR / sample["filename"]).read_bytes()
        assert payload.startswith(b"%PDF-")
        assert payload.rstrip().endswith(b"%%EOF")

        has_document_structure = all(
            marker in payload
            for marker in (b"xref\n", b"trailer\n", b"/Type /Catalog", b"/Type /Pages")
        )
        assert has_document_structure is sample["valid_format"]

        if sample["valid_format"]:
            declared_pages = sample["page_count"]
            assert declared_pages is not None
            assert payload.count(b"/Type /Page ") == declared_pages
            assert payload.count(b"\nendobj\n") >= 5

    image_payload = (FIXTURE_DIR / "image_only.pdf").read_bytes()
    assert b"/Subtype /Image" in image_payload
    assert b"BT\n" not in image_payload
    assert b" Tj" not in image_payload


def test_expected_quotes_are_present_in_source_backed_fixtures() -> None:
    manifest = _load_manifest()

    for sample in manifest["samples"]:
        if sample["format"] not in {"markdown", "text", "html"}:
            continue

        source_text = (FIXTURE_DIR / sample["filename"]).read_text(encoding="utf-8")
        positions = [source_text.index(quote["text"]) for quote in sample["ordered_quotes"]]
        assert positions == sorted(positions)

        for excluded_text in sample.get("excluded_text", []):
            assert excluded_text in source_text
            assert excluded_text not in {
                quote["text"] for quote in sample["ordered_quotes"]
            }
