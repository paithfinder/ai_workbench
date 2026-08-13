#!/usr/bin/env python3
"""Run the D8 hybrid QA seed set against a live trusted-QA API."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = BASE_DIR / "rag-seeds.zh-CN.json"
DEFAULT_ENDPOINT = "/api/v1/knowledge-spaces/{space_id}/qa/turns"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--space-id", required=True)
    parser.add_argument("--fixture-map", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--allow-fake-smoke", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"{path} root must be an object")
    return value


def load_dataset(path: Path) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value.get("seeds"), list) or not value["seeds"]:
        raise TypeError("dataset root must contain a non-empty seeds array")
    return value


def load_fixture_map(path: Path) -> dict[str, Any]:
    value = load_json(path)
    mappings = value.get("mappings")
    if not isinstance(mappings, dict):
        raise TypeError("fixture map must contain mappings")
    for kind in ("scope_node_ids", "content_identities", "section_ids"):
        if not isinstance(mappings.get(kind), dict):
            raise TypeError(f"fixture map {kind} must be an object")
    return value


def assert_complete_fixture_map(fixture_map: dict[str, Any], seeds: list[dict[str, Any]]) -> None:
    mappings = fixture_map["mappings"]
    missing: list[str] = []
    for seed in seeds:
        scope_key = seed["scope"]["key"]
        if scope_key not in mappings["scope_node_ids"]:
            missing.append(f"scope_node_ids:{scope_key}")
        for field, kind in (
            ("expected_content_identities", "content_identities"),
            ("out_of_scope_content_identities", "content_identities"),
            ("expected_section_ids", "section_ids"),
        ):
            for key in seed[field]:
                if key not in mappings[kind]:
                    missing.append(f"{kind}:{key}")
    if missing:
        raise RuntimeError("fixture map is incomplete: " + ", ".join(sorted(set(missing))[:10]))


def post_json(url: str, body: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from QA API: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"QA API request failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise TypeError("QA API response must be an object")
    return payload


def request_body(seed: dict[str, Any], mappings: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "question": seed["question"],
        "scope": {
            "scope_node_id": mappings["scope_node_ids"][seed["scope"]["key"]],
            "include_descendants": seed["scope"]["include_descendants"],
        },
    }


def stable_key(dataset: dict[str, Any], seed_id: str, run_id: str) -> str:
    namespace = UUID("903cbcb8-d00f-4aa6-8717-6ccbedafbbd7")
    identity = f"{dataset.get('dataset')}:{dataset.get('version')}:{run_id}:{seed_id}"
    return f"d8-eval-{uuid5(namespace, identity)}"


def validate_turn(payload: dict[str, Any]) -> None:
    required = {
        "id", "status", "question", "scope_snapshot", "index_config_version",
        "ai_provider", "ai_model", "answer", "abstain_code", "error_code",
        "error_message", "warnings", "claims", "citations",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise RuntimeError("QA response missing fields: " + ", ".join(missing))
    if payload["status"] not in {"answered", "abstained", "failed", "processing"}:
        raise RuntimeError(f"invalid QA status: {payload['status']!r}")
    if not isinstance(payload["claims"], list):
        raise TypeError("QA claims must be an array")
    if not isinstance(payload["citations"], list):
        raise TypeError("QA citations must be an array")


def score_seed(
    seed: dict[str, Any], payload: dict[str, Any], mappings: dict[str, dict[str, str]]
) -> dict[str, Any]:
    validate_turn(payload)
    citations = payload["citations"]
    expected_identities = {
        mappings["content_identities"][key] for key in seed["expected_content_identities"]
    }
    expected_sections = {
        mappings["section_ids"][key] for key in seed["expected_section_ids"]
    }
    out_of_scope = {
        mappings["content_identities"][key]
        for key in seed["out_of_scope_content_identities"]
    }
    cited_identities = {str(item.get("content_identity")) for item in citations}
    cited_sections = {
        str(item["section_id"]) for item in citations if item.get("section_id") is not None
    }
    leaked = sorted(cited_identities & out_of_scope)
    citation_valid = all(
        isinstance(item, dict)
        and item.get("evidence_id")
        and item.get("claim_id")
        and item.get("claim_text")
        and item.get("frozen_quote")
        and item.get("content_identity")
        and item.get("corpus_kind") in {"source_evidence", "confirmed_knowledge"}
        for item in citations
    ) and not leaked
    claims = payload["claims"]
    claim_ids = {
        str(item["claim_id"])
        for item in claims
        if isinstance(item, dict) and item.get("claim_id")
    }
    covered_claims = {
        str(item["claim_id"])
        for item in citations
        if isinstance(item, dict) and item.get("claim_id") and item.get("evidence_id")
    }
    claim_coverage = (
        len(claim_ids & covered_claims) / len(claim_ids)
        if claim_ids
        else (1.0 if seed["should_abstain"] else 0.0)
    )
    matched_identities = cited_identities & expected_identities
    matched_sections = cited_sections & expected_sections
    all_expected_evidence_cited = (
        expected_identities <= cited_identities and expected_sections <= cited_sections
    )
    correct_abstention = payload["status"] == ("abstained" if seed["should_abstain"] else "answered")
    return {
        "id": seed["id"],
        "expected_status": "abstained" if seed["should_abstain"] else "answered",
        "actual_status": payload["status"],
        "correct_abstention": correct_abstention,
        "citation_validity": 1.0 if citation_valid else 0.0,
        "claim_citation_coverage": claim_coverage,
        "expected_evidence_cited": all_expected_evidence_cited if not seed["should_abstain"] else None,
        "matched_expected_identities": len(matched_identities),
        "matched_expected_sections": len(matched_sections),
        "scope_leak_count": len(leaked),
        "citation_count": len(citations),
        "warnings": payload.get("warnings", []),
    }


def runtime_metadata(fixture_map: dict[str, Any], payloads: list[dict[str, Any]]) -> dict[str, Any]:
    providers = sorted({str(item["ai_provider"]) for item in payloads})
    models = sorted({str(item["ai_model"]) for item in payloads})
    index_versions = sorted({str(item["index_config_version"]) for item in payloads})
    return {
        "embedding": fixture_map.get("embedding"),
        "index_config_version": fixture_map.get("index_config_version"),
        "qa_providers": providers,
        "qa_models": models,
        "turn_index_config_versions": index_versions,
    }


def baseline_metadata_issues(metadata: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    embedding = metadata.get("embedding")
    if not isinstance(embedding, dict):
        return ["fixture embedding metadata is missing"]
    if embedding.get("provider") != "bge_m3_http":
        issues.append("embedding.provider must be bge_m3_http")
    if "bge-m3" not in str(embedding.get("model", "")).lower():
        issues.append("embedding.model must identify BGE-M3")
    if embedding.get("dimensions") != 1024:
        issues.append("embedding.dimensions must be 1024")
    index_version = metadata.get("index_config_version")
    if not isinstance(index_version, str) or not index_version.strip():
        issues.append("fixture index_config_version is missing")
    if metadata.get("turn_index_config_versions") != [index_version]:
        issues.append("QA turn index_config_version must match fixture metadata")
    return issues


def run(args: argparse.Namespace) -> dict[str, Any]:
    dataset = load_dataset(args.dataset)
    seeds: list[dict[str, Any]] = dataset["seeds"]
    fixture_map = load_fixture_map(args.fixture_map)
    assert_complete_fixture_map(fixture_map, seeds)
    if str(fixture_map.get("space_id")) != str(args.space_id):
        raise RuntimeError("--space-id does not match fixture map")
    embedding = fixture_map.get("embedding", {})
    fake_embedding = "fake" in str(embedding).lower()
    if fake_embedding and not args.allow_fake_smoke:
        raise RuntimeError("Fake embedding is allowed only with --allow-fake-smoke")
    mappings: dict[str, dict[str, str]] = fixture_map["mappings"]
    endpoint = args.endpoint.format(space_id=args.space_id)
    url = f"{args.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    payloads: list[dict[str, Any]] = []
    per_seed: list[dict[str, Any]] = []
    started = time.perf_counter()
    run_id = str(uuid4())
    for seed in seeds:
        payload = post_json(
            url,
            request_body(seed, mappings),
            stable_key(dataset, seed["id"], run_id),
            args.timeout,
        )
        payloads.append(payload)
        per_seed.append(score_seed(seed, payload, mappings))
    answered = [item for item in per_seed if item["actual_status"] == "answered"]
    abstention_cases = [item for item in per_seed if item["expected_status"] == "abstained"]
    leaked_hits = sum(item["scope_leak_count"] for item in per_seed)
    total_citations = sum(item["citation_count"] for item in per_seed)
    citation_validity = (
        sum(item["citation_validity"] * item["citation_count"] for item in per_seed) / total_citations
        if total_citations else 1.0
    )
    claim_coverage = (
        sum(item["claim_citation_coverage"] for item in answered) / len(answered)
        if answered else None
    )
    correct_abstention = (
        sum(item["correct_abstention"] for item in abstention_cases) / len(abstention_cases)
        if abstention_cases else None
    )
    failures = [
        item for item in per_seed
        if not item["correct_abstention"]
        or item["citation_validity"] != 1.0
        or item["claim_citation_coverage"] != 1.0
        or item["scope_leak_count"]
        or item["expected_evidence_cited"] is False
    ]
    metadata = runtime_metadata(fixture_map, payloads)
    metadata_issues = baseline_metadata_issues(metadata)
    qa_fake = any("fake" in value.lower() for value in [*metadata["qa_providers"], *metadata["qa_models"]])
    if qa_fake and not args.allow_fake_smoke:
        raise RuntimeError("Fake QA provider/model is allowed only with --allow-fake-smoke")
    if not fake_embedding and metadata_issues:
        raise RuntimeError("QA baseline ineligible: " + "; ".join(metadata_issues))
    baseline_eligible = not fake_embedding and not qa_fake and not metadata_issues
    return {
        "dataset": dataset.get("dataset"),
        "dataset_version": dataset.get("version"),
        "seed_count": len(seeds),
        "run_id": run_id,
        "endpoint": url,
        "fixture_map": str(args.fixture_map),
        "run_kind": "qa-baseline" if baseline_eligible else "fake-smoke",
        "baseline_eligible": baseline_eligible,
        "runtime": metadata,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "metrics": {
            "citation_validity": citation_validity,
            "claim_citation_coverage": claim_coverage,
            "correct_abstention_rate": correct_abstention,
            "scope_leakage": leaked_hits / total_citations if total_citations else 0.0,
            "scope_leaked_citations": leaked_hits,
            "answered_turns": len(answered),
        },
        "failures": failures,
        "per_seed": per_seed,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run(args)
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print(f"QA eval failed: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
