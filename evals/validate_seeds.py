#!/usr/bin/env python3
"""Validate the D7 retrieval seeds against their JSON Schema without extra packages."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "rag-seeds.zh-CN.json"
SCHEMA_PATH = BASE_DIR / "rag-seeds.schema.json"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"仅支持本地 JSON Pointer：{reference}")
    current: Any = root_schema
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        current = current[part]
    if not isinstance(current, dict):
        raise TypeError(f"$ref 未指向 schema 对象：{reference}")
    return current


def matches_type(value: object, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    return checks[expected](value)


def validate_schema(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str = "$",
) -> list[str]:
    if "$ref" in schema:
        schema = resolve_ref(root_schema, schema["$ref"])

    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type and not matches_type(value, expected_type):
        return [f"{path} 必须是 {expected_type}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} 必须等于 {schema['const']!r}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path} 缺少必填字段 {key}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value.keys() - properties.keys():
                errors.append(f"{path} 包含未声明字段 {key}")
        for key, child in properties.items():
            if key in value:
                errors.extend(validate_schema(value[key], child, root_schema, f"{path}.{key}"))

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path} 条目数少于 {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} 条目数超过 {schema['maxItems']}")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            errors.append(f"{path} 包含重复条目")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, item_schema, root_schema, f"{path}[{index}]"))

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path} 长度少于 {schema['minLength']}")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            errors.append(f"{path} 不符合模式 {schema['pattern']}")

    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and "minimum" in schema
        and value < schema["minimum"]
    ):
        errors.append(f"{path} 小于最小值 {schema['minimum']}")

    return errors


def validate_semantics(dataset: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seeds = dataset.get("seeds", [])
    ids = [seed.get("id") for seed in seeds if isinstance(seed, dict)]
    if len(ids) != len(set(ids)):
        errors.append("$.seeds 中的 id 必须唯一")
    for index, seed in enumerate(seeds):
        if not isinstance(seed, dict):
            continue
        contexts = seed.get("contexts", [])
        for citation in seed.get("expected_citations", []):
            if isinstance(citation, int) and not 0 <= citation < len(contexts):
                errors.append(f"$.seeds[{index}].expected_citations 包含越界索引 {citation}")
        positives = set(seed.get("expected_content_identities", []))
        sections = seed.get("expected_section_ids", [])
        negatives = set(seed.get("difficult_negative_content_identities", []))
        out_of_scope = set(seed.get("out_of_scope_content_identities", []))
        overlap = positives & (negatives | out_of_scope)
        if overlap:
            errors.append(
                f"$.seeds[{index}] 正样本与负样本重叠：{sorted(overlap)}"
            )
        should_abstain = seed.get("should_abstain")
        if should_abstain:
            if positives or sections:
                errors.append(
                    f"$.seeds[{index}] should_abstain=true 时正样本与 section 必须为空"
                )
            if "拒答" not in seed.get("tags", []):
                errors.append(f"$.seeds[{index}] should_abstain=true 时 tags 必须包含拒答")
        elif not positives or not sections:
            errors.append(
                f"$.seeds[{index}] should_abstain=false 时正样本与 section 不得为空"
            )
        elif len(positives) != len(sections):
            errors.append(
                f"$.seeds[{index}] 正样本 identity 与 section 数量必须一致"
            )
        if seed.get("allow_general_supplement") is not False:
            errors.append(f"$.seeds[{index}].allow_general_supplement 在 D7 必须为 false")
    return errors


def main() -> int:
    try:
        schema = load_json(SCHEMA_PATH)
        dataset = load_json(DATA_PATH)
        if not isinstance(schema, dict) or not isinstance(dataset, dict):
            raise TypeError("schema 与数据集根节点必须是对象")
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("schema 必须声明 JSON Schema Draft 2020-12")
        errors = validate_schema(dataset, schema, schema) + validate_semantics(dataset)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"校验器执行失败：{exc}", file=sys.stderr)
        return 1

    if errors:
        print("RAG eval seeds 校验失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    seeds = dataset.get("seeds", [])
    statuses = sorted(
        {seed.get("status") for seed in seeds if isinstance(seed, dict)}
    )
    print(
        f"RAG eval seeds 校验通过：{len(seeds)} 条，"
        f"status={','.join(str(item) for item in statuses)}。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
