#!/usr/bin/env python3
"""Run the D7 retrieval seed set against a live HTTP debug API."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = BASE_DIR / "rag-seeds.zh-CN.json"
DEFAULT_ENDPOINT = "/api/v1/knowledge-spaces/{space_id}/retrieval/debug-search"
MODES = ("keyword", "vector")


@dataclass(frozen=True)
class Hit:
    identity: str | None
    section_id: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="API origin, e.g. http://localhost:8000")
    parser.add_argument("--space-id", required=True, help="real seeded knowledge-space UUID")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--fixture-map",
        type=Path,
        help="required for real baselines; maps stable fixture keys to indexed IDs",
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="path template containing {space_id}")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--mrr", action="store_true", help="include MRR in the report")
    parser.add_argument(
        "--request-style",
        choices=("combined", "per-mode"),
        default="combined",
        help="combined expects keyword_results/vector_results; per-mode sends two requests",
    )
    parser.add_argument("--allow-fake-smoke", action="store_true", help="allow fake only as non-baseline smoke")
    parser.add_argument(
        "--smoke-scope-node-id",
        help="real scope-node UUID required for fake smoke without a fixture map",
    )
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    return parser.parse_args(argv)


def load_dataset(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or not isinstance(value.get("seeds"), list):
        raise TypeError("dataset root must contain a seeds array")
    selected = [seed for seed in value["seeds"] if not seed.get("should_abstain")]
    if not selected:
        raise ValueError("dataset has no answerable retrieval seeds")
    value["seeds"] = selected
    return value


def load_fixture_map(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError("fixture map root must be an object")
    mappings = value.get("mappings", value)
    if not isinstance(mappings, dict):
        raise TypeError("fixture map mappings must be an object")
    normalized: dict[str, dict[str, str]] = {}
    for kind in ("scope_node_ids", "content_identities", "section_ids"):
        entries = mappings.get(kind, {})
        if not isinstance(entries, dict) or not all(
            isinstance(key, str)
            and isinstance(item, str)
            and item
            and not item.startswith("<")
            for key, item in entries.items()
        ):
            raise ValueError(
                f"fixture map {kind} must map keys to non-placeholder strings"
            )
        normalized[kind] = entries
    return normalized


def map_key(mapping: dict[str, dict[str, str]], kind: str, key: str) -> str:
    return mapping.get(kind, {}).get(key, key)


def required_fixture_keys(seeds: list[dict[str, Any]]) -> dict[str, set[str]]:
    keys = {"scope_node_ids": set(), "content_identities": set(), "section_ids": set()}
    for seed in seeds:
        if seed["should_abstain"]:
            continue
        keys["scope_node_ids"].add(seed["scope"]["key"])
        for field, kind in (
            ("expected_content_identities", "content_identities"),
            ("out_of_scope_content_identities", "content_identities"),
            ("expected_section_ids", "section_ids"),
        ):
            keys[kind].update(seed[field])
    return keys


def assert_complete_fixture_map(
    mapping: dict[str, dict[str, str]], seeds: list[dict[str, Any]]
) -> None:
    missing: list[str] = []
    for kind, keys in required_fixture_keys(seeds).items():
        unresolved = sorted(key for key in keys if key not in mapping.get(kind, {}))
        if unresolved:
            missing.append(f"{kind}: {', '.join(unresolved[:5])}" + (" ..." if len(unresolved) > 5 else ""))
    if missing:
        raise RuntimeError("fixture map is incomplete: " + "; ".join(missing))


def apply_fixture_map(seed: dict[str, Any], mapping: dict[str, dict[str, str]]) -> dict[str, Any]:
    resolved = dict(seed)
    if seed["should_abstain"]:
        return resolved
    resolved["scope"] = dict(seed["scope"])
    resolved["scope"]["key"] = map_key(mapping, "scope_node_ids", seed["scope"]["key"])
    for field, kind in (
        ("expected_content_identities", "content_identities"),
        ("out_of_scope_content_identities", "content_identities"),
        ("expected_section_ids", "section_ids"),
    ):
        resolved[field] = [map_key(mapping, kind, value) for value in seed[field]]
    return resolved


def apply_smoke_scope(
    seed: dict[str, Any], smoke_scope_node_id: str
) -> dict[str, Any]:
    resolved = dict(seed)
    resolved["scope"] = dict(seed["scope"])
    resolved["scope"]["key"] = smoke_scope_node_id
    return resolved


def post_json(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from debug API: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"debug API request failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise TypeError("debug API response must be a JSON object")
    return payload


def request_body(seed: dict[str, Any], top_k: int, mode: str | None = None) -> dict[str, Any]:
    scope = seed["scope"]
    body: dict[str, Any] = {
        "query": seed["question"],
        "scope": {
            "scope_node_id": scope["key"],
            "include_descendants": scope["include_descendants"],
        },
        "top_k": top_k,
        "channels": ["keyword", "vector"],
    }
    if mode is not None:
        body["channels"] = [mode]
    return body


def nested(value: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = value
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            return current
    return None


def has_channel_errors(value: Any) -> bool:
    if value in ({}, [], None):
        return False
    if isinstance(value, dict):
        return any(item not in (None, "", [], {}) for item in value.values())
    return True


def result_rows(payload: dict[str, Any], mode: str, request_style: str) -> list[dict[str, Any]]:
    if request_style == "per-mode":
        rows = nested(payload, ("results",), ("hits",), ("items",))
    else:
        rows = nested(
            payload,
            (f"{mode}_hits",),
            (f"{mode}_results",),
            ("results", mode),
            ("results", f"{mode}_results"),
            (mode, "results"),
        )
    if not isinstance(rows, list):
        raise TypeError(f"debug API response has no {mode} result list")
    if not all(isinstance(item, dict) for item in rows):
        raise RuntimeError(f"{mode} result list must contain objects")
    return rows


def parse_hit(row: dict[str, Any]) -> Hit:
    identity = nested(
        row,
        ("content_identity",),
        ("knowledge_identity",),
        ("knowledge_id",),
        ("content", "identity"),
        ("knowledge", "id"),
    )
    section_id = nested(row, ("section_id",), ("section", "id"))
    return Hit(
        str(identity) if identity is not None else None,
        str(section_id) if section_id is not None else None,
    )


def runtime_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    embedding = payload.get("embedding")
    if not isinstance(embedding, dict):
        embedding = {}
    metadata = nested(payload, ("metadata",), ("debug",), ("retrieval", "metadata"))
    if not isinstance(metadata, dict):
        metadata = {}
    provider = nested(
        payload,
        ("embedding", "provider"),
        ("provider",),
        ("embedding_provider",),
        ("metadata", "provider"),
        ("metadata", "embedding_provider"),
        ("debug", "provider"),
    )
    model = nested(
        payload,
        ("embedding", "model"),
        ("model",),
        ("embedding_model",),
        ("metadata", "model"),
        ("metadata", "embedding_model"),
        ("debug", "model"),
    )
    dimensions = nested(payload, ("embedding", "dimensions"), ("dimensions",))
    index_config_version = nested(payload, ("scope_summary", "index_config_version"))
    return {
        "provider": provider if provider is not None else embedding.get("provider", metadata.get("provider", "unknown")),
        "model": model if model is not None else embedding.get("model", metadata.get("model", "unknown")),
        "dimensions": dimensions,
        "index_config_version": index_config_version,
        "config": {
            "dimensions": dimensions,
            "index_config_version": index_config_version,
        },
    }


def validate_scope_summary(payload: dict[str, Any], seed: dict[str, Any]) -> None:
    summary = payload.get("scope_summary")
    if not isinstance(summary, dict):
        raise TypeError("debug API response has no scope_summary object")
    expected_scope = seed["scope"]
    if str(summary.get("scope_node_id")) != str(expected_scope["key"]):
        raise RuntimeError(
            f"scope_summary.scope_node_id mismatch for {seed['id']}: "
            f"{summary.get('scope_node_id')!r}"
        )
    if summary.get("include_descendants") is not expected_scope["include_descendants"]:
        raise RuntimeError(
            f"scope_summary.include_descendants mismatch for {seed['id']}"
        )
    required_snapshot_fields = {
        "scope_snapshot_hash",
        "scope_path",
        "node_kind",
        "node_title",
        "knowledge_count",
        "source_count",
        "source_version_count",
        "chunk_count",
        "index_status",
        "index_config_version",
    }
    missing = sorted(required_snapshot_fields - summary.keys())
    if missing:
        raise RuntimeError(f"scope_summary missing fields: {', '.join(missing)}")


def baseline_metadata_issues(metadata: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    provider = str(metadata.get("provider", "")).strip().lower()
    model = str(metadata.get("model", "")).strip().lower()
    if provider != "bge_m3_http":
        issues.append("embedding.provider must be bge_m3_http")
    if "bge-m3" not in model:
        issues.append("embedding.model must identify bge-m3")
    if metadata.get("dimensions") != 1024:
        issues.append("embedding.dimensions must be 1024")
    index_config_version = metadata.get("index_config_version")
    if not isinstance(index_config_version, str) or not index_config_version.strip():
        issues.append("scope_summary.index_config_version is missing")
    return issues


def is_fake(metadata: dict[str, Any]) -> bool:
    values = (metadata.get("provider"), metadata.get("model"))
    return any("fake" in str(value).lower() for value in values)


def reciprocal_rank(hits: list[Hit], expected: set[str], expected_sections: set[str]) -> float:
    for rank, hit in enumerate(hits, 1):
        if hit.identity in expected or hit.section_id in expected_sections:
            return 1.0 / rank
    return 0.0


def evaluate_mode(seeds: list[dict[str, Any]], hits_by_seed: dict[str, list[Hit]], top_k: int) -> dict[str, Any]:
    recall_scores: list[float] = []
    reciprocal_ranks: list[float] = []
    leaked_hits = 0
    returned_hits = 0
    per_seed: list[dict[str, Any]] = []
    for seed in seeds:
        hits = hits_by_seed[seed["id"]][:top_k]
        expected = list(seed["expected_content_identities"])
        expected_sections = list(seed["expected_section_ids"])
        expected_id_set = set(expected)
        expected_section_set = set(expected_sections)
        out_of_scope = set(seed["out_of_scope_content_identities"])
        relevant_units = list(zip(expected, expected_sections, strict=True))
        matched_units = sum(
            any(
                hit.identity == identity or hit.section_id == section
                for hit in hits
            )
            for identity, section in relevant_units
        )
        if relevant_units:
            recall = matched_units / len(relevant_units)
            recall_scores.append(recall)
            rr = reciprocal_rank(hits, expected_id_set, expected_section_set)
            reciprocal_ranks.append(rr)
        else:
            recall = None
            rr = None
        leak_count = sum(hit.identity in out_of_scope for hit in hits)
        leaked_hits += leak_count
        returned_hits += len(hits)
        per_seed.append({
            "id": seed["id"],
            "should_abstain": seed["should_abstain"],
            "recall_at_k": recall,
            "reciprocal_rank": rr,
            "scope_leak_count": leak_count,
            "returned": len(hits),
        })
    return {
        "recall_at_k": sum(recall_scores) / len(recall_scores) if recall_scores else None,
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else None,
        "scope_leakage": leaked_hits / returned_hits if returned_hits else 0.0,
        "scope_leaked_hits": leaked_hits,
        "returned_hits": returned_hits,
        "evaluated_queries": len(recall_scores),
        "per_seed": per_seed,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.top_k < 1:
        raise ValueError("--top-k must be positive")
    dataset = load_dataset(args.dataset)
    seeds: list[dict[str, Any]] = dataset["seeds"]
    fixture_map = load_fixture_map(args.fixture_map)
    fake_smoke_without_map = args.allow_fake_smoke and not fixture_map
    if fake_smoke_without_map:
        if not args.smoke_scope_node_id:
            raise RuntimeError(
                "--smoke-scope-node-id is required for fake smoke without a fixture map"
            )
        try:
            smoke_scope_node_id = str(UUID(args.smoke_scope_node_id))
        except ValueError as exc:
            raise ValueError("--smoke-scope-node-id must be a UUID") from exc
    else:
        smoke_scope_node_id = None
        if args.fixture_map is None:
            raise RuntimeError("--fixture-map is required for a real baseline")
        assert_complete_fixture_map(fixture_map, seeds)
    resolved_seeds = [
        apply_smoke_scope(seed, smoke_scope_node_id)
        if smoke_scope_node_id is not None
        else apply_fixture_map(seed, fixture_map)
        for seed in seeds
    ]
    endpoint = args.endpoint.format(space_id=args.space_id)
    url = f"{args.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    hits_by_mode: dict[str, dict[str, list[Hit]]] = {mode: {} for mode in MODES}
    metadata: dict[str, Any] | None = None
    started = time.perf_counter()
    for seed in resolved_seeds:
        if args.request_style == "combined":
            payloads = {mode: None for mode in MODES}
            combined = post_json(url, request_body(seed, args.top_k), args.timeout)
            validate_scope_summary(combined, seed)
            channel_errors = combined.get("channel_errors", {})
            if has_channel_errors(channel_errors):
                raise RuntimeError(
                    f"debug API returned channel_errors for {seed['id']}: {channel_errors}"
                )
            for mode in MODES:
                payloads[mode] = combined
        else:
            payloads = {
                mode: post_json(
                    url, request_body(seed, args.top_k, mode), args.timeout
                )
                for mode in MODES
            }
            for payload in payloads.values():
                validate_scope_summary(payload, seed)
                channel_errors = payload.get("channel_errors", {})
                if has_channel_errors(channel_errors):
                    raise RuntimeError(
                        f"debug API returned channel_errors for {seed['id']}: {channel_errors}"
                    )
        for mode, payload in payloads.items():
            assert payload is not None
            current_metadata = runtime_metadata(payload)
            if metadata is None:
                metadata = current_metadata
                if is_fake(metadata) and not args.allow_fake_smoke:
                    raise RuntimeError(
                        "fake retrieval provider/model cannot be reported as a real baseline; "
                        "use --allow-fake-smoke only for transport smoke tests"
                    )
            elif current_metadata != metadata:
                raise RuntimeError("embedding metadata changed during one evaluation run")
            hits_by_mode[mode][seed["id"]] = [
                parse_hit(row)
                for row in result_rows(payload, mode, args.request_style)[: args.top_k]
            ]
    assert metadata is not None
    fake = is_fake(metadata)
    metadata_issues = baseline_metadata_issues(metadata)
    if not fake and metadata_issues:
        raise RuntimeError("real baseline ineligible: " + "; ".join(metadata_issues))
    baseline_eligible = not fake and not metadata_issues and bool(fixture_map)
    metrics = {
        mode: evaluate_mode(resolved_seeds, hits_by_mode[mode], args.top_k) for mode in MODES
    }
    if not args.mrr:
        for mode in MODES:
            metrics[mode].pop("mrr")
            for item in metrics[mode]["per_seed"]:
                item.pop("reciprocal_rank")
    return {
        "dataset": dataset.get("dataset"),
        "dataset_version": dataset.get("version"),
        "seed_count": len(seeds),
        "fixture_map": str(args.fixture_map) if args.fixture_map else None,
        "top_k": args.top_k,
        "endpoint": url,
        "request_style": args.request_style,
        "runtime": metadata,
        "baseline_eligible": baseline_eligible,
        "run_kind": "real-baseline" if baseline_eligible else "fake-smoke",
        "duration_seconds": round(time.perf_counter() - started, 3),
        "metrics": metrics,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run(args)
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print(f"retrieval eval failed: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
