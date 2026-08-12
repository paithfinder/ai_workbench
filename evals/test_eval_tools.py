from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

EVALS_DIR = Path(__file__).resolve().parent


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, EVALS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validator = load_module("validate_seeds", "validate_seeds.py")
runner = load_module("run_retrieval", "run_retrieval.py")
fixture = load_module("prepare_retrieval_fixture", "prepare_retrieval_fixture.py")


class ValidatorTests(unittest.TestCase):
    def test_checked_in_dataset_passes_schema_and_semantics(self) -> None:
        schema = validator.load_json(validator.SCHEMA_PATH)
        dataset = validator.load_json(validator.DATA_PATH)
        errors = validator.validate_schema(dataset, schema, schema)
        errors.extend(validator.validate_semantics(dataset))
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(dataset["seeds"]), 30)

    def test_abstention_must_not_declare_retrieval_positives(self) -> None:
        seed = {
            "id": "rag-zh-999",
            "contexts": ["背景"],
            "expected_citations": [0],
            "expected_content_identities": ["content:wrong"],
            "expected_section_ids": ["section-wrong"],
            "difficult_negative_content_identities": ["content:negative"],
            "out_of_scope_content_identities": ["content:outside"],
            "should_abstain": True,
            "allow_general_supplement": False,
            "tags": ["拒答"],
        }
        errors = validator.validate_semantics({"seeds": [seed]})
        self.assertTrue(any("必须为空" in error for error in errors))


class FixturePreparationTests(unittest.TestCase):
    def test_checked_in_dataset_has_deterministic_fixture_units(self) -> None:
        dataset = fixture._load_dataset(fixture.DEFAULT_DATASET)
        units = fixture.fixture_units(dataset)
        positives = [unit for unit in units if not unit.outside]
        outside = [unit for unit in units if unit.outside]

        self.assertEqual(len(positives), 28)
        self.assertEqual(len(outside), 27)
        self.assertEqual(len({unit.content_key for unit in units}), 55)
        self.assertEqual(len({unit.section_key for unit in positives}), 28)
        self.assertEqual(
            {unit.scope_key for unit in outside}, {fixture.OUTSIDE_SCOPE_KEY}
        )

    def test_multiple_contexts_merge_for_one_expected_identity(self) -> None:
        dataset = {
            "dataset": "test",
            "version": "1.0.0",
            "seeds": [
                {
                    "id": "rag-test-1",
                    "question": "问题",
                    "contexts": ["第一段", "第二段"],
                    "scope": {"key": "scope-a", "include_descendants": True},
                    "expected_content_identities": ["content-a"],
                    "expected_section_ids": ["section-a"],
                    "out_of_scope_content_identities": ["outside-a"],
                    "should_abstain": False,
                }
            ],
        }
        units = fixture.fixture_units(dataset)
        positive = next(unit for unit in units if not unit.outside)
        outside = next(unit for unit in units if unit.outside)

        self.assertEqual(positive.text, "第一段\n第二段")
        self.assertIn("问题", outside.text)
        self.assertEqual(outside.scope_key, fixture.OUTSIDE_SCOPE_KEY)

    def test_fixture_ids_are_stable_and_version_scoped(self) -> None:
        dataset = {"dataset": "test", "version": "1.0.0"}
        same = fixture._stable_uuid(dataset, "section", "section-a")
        self.assertEqual(same, fixture._stable_uuid(dataset, "section", "section-a"))
        changed = dict(dataset, version="1.0.1")
        self.assertNotEqual(same, fixture._stable_uuid(changed, "section", "section-a"))


class RunnerTests(unittest.TestCase):
    def seed(self) -> dict[str, object]:
        return {
            "id": "rag-zh-901",
            "question": "测试问题是什么？",
            "scope": {"key": "scope-a", "include_descendants": True},
            "expected_content_identities": ["content:expected"],
            "expected_section_ids": ["section-expected"],
            "difficult_negative_content_identities": ["content:negative"],
            "out_of_scope_content_identities": ["content:outside"],
            "should_abstain": False,
        }

    def args(
        self, dataset: Path, allow_fake: bool = False, fixture_map: Path | None = None
    ) -> argparse.Namespace:
        return argparse.Namespace(
            base_url="http://localhost:8000",
            space_id="space-1",
            dataset=dataset,
            fixture_map=fixture_map,
            endpoint=runner.DEFAULT_ENDPOINT,
            top_k=5,
            timeout=1.0,
            mrr=True,
            request_style="combined",
            allow_fake_smoke=allow_fake,
            smoke_scope_node_id=None,
            output=None,
        )

    def write_dataset(self, directory: str) -> Path:
        path = Path(directory) / "seeds.json"
        path.write_text(
            json.dumps({"dataset": "test", "version": "1.0.0", "seeds": [self.seed()]}),
            encoding="utf-8",
        )
        return path

    def write_fixture_map(self, directory: str) -> Path:
        path = Path(directory) / "fixture-map.json"
        path.write_text(
            json.dumps({
                "mappings": {
                    "scope_node_ids": {"scope-a": "scope-a"},
                    "content_identities": {
                        "content:expected": "content:expected",
                        "content:outside": "content:outside",
                    },
                    "section_ids": {"section-expected": "section-expected"},
                }
            }),
            encoding="utf-8",
        )
        return path

    def test_request_matches_debug_search_contract(self) -> None:
        body = runner.request_body(self.seed(), 5)
        self.assertEqual(
            body,
            {
                "query": "测试问题是什么？",
                "scope": {"scope_node_id": "scope-a", "include_descendants": True},
                "top_k": 5,
                "channels": ["keyword", "vector"],
            },
        )

    def test_real_response_computes_recall_mrr_and_scope_leakage(self) -> None:
        response = {
            "query": "测试问题是什么？",
            "scope_summary": {
                "scope_node_id": "scope-a",
                "include_descendants": True,
                "scope_snapshot_hash": "sha256:test",
                "scope_path": "root.scope-a",
                "node_kind": "folder",
                "node_title": "Scope A",
                "knowledge_count": 1,
                "source_count": 1,
                "source_version_count": 1,
                "chunk_count": 2,
                "index_status": "ready",
                "index_config_version": "v1",
            },
            "embedding": {
                "provider": "bge_m3_http",
                "model": "BAAI/bge-m3",
                "dimensions": 1024,
            },
            "keyword_hits": [
                {
                    "content_identity": "content:expected",
                    "section_id": "section-expected",
                    "rank": 1,
                    "raw_score": 1.0,
                }
            ],
            "vector_hits": [
                {
                    "content_identity": "content:outside",
                    "section_id": "section-other",
                    "rank": 1,
                    "raw_score": 0.9,
                },
                {
                    "content_identity": "content:expected",
                    "section_id": "section-expected",
                    "rank": 2,
                    "raw_score": 0.8,
                },
            ],
            "channel_errors": {},
            "timings_ms": {"total": 12.0},
        }
        with tempfile.TemporaryDirectory() as directory:
            dataset = self.write_dataset(directory)
            fixture_map = self.write_fixture_map(directory)
            with patch.object(runner, "post_json", return_value=response):
                report = runner.run(self.args(dataset, fixture_map=fixture_map))
        self.assertEqual(report["runtime"]["model"], "BAAI/bge-m3")
        self.assertTrue(report["baseline_eligible"])
        self.assertEqual(report["metrics"]["keyword"]["recall_at_k"], 1.0)
        self.assertEqual(report["metrics"]["vector"]["mrr"], 0.5)
        self.assertEqual(report["metrics"]["vector"]["scope_leaked_hits"], 1)

    def test_abstention_seeds_are_not_sent_to_retrieval_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seeds.json"
            answerable = self.seed()
            abstention = dict(answerable)
            abstention.update({
                "id": "rag-zh-902",
                "should_abstain": True,
                "expected_content_identities": [],
                "expected_section_ids": [],
            })
            path.write_text(
                json.dumps({"seeds": [answerable, abstention]}), encoding="utf-8"
            )
            loaded = runner.load_dataset(path)
        self.assertEqual([seed["id"] for seed in loaded["seeds"]], ["rag-zh-901"])

    def test_non_bge_provider_is_not_baseline_eligible(self) -> None:
        metadata = {
            "provider": "openai-compatible",
            "model": "BAAI/bge-m3",
            "dimensions": 1024,
            "index_config_version": "v1",
        }
        self.assertIn(
            "embedding.provider must be bge_m3_http",
            runner.baseline_metadata_issues(metadata),
        )

    def test_fixture_map_is_required_and_must_be_complete(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "fixture map is incomplete"):
            runner.assert_complete_fixture_map(
                {"scope_node_ids": {}, "content_identities": {}, "section_ids": {}},
                [self.seed()],
            )

    def test_channel_errors_ignores_null_channel_values(self) -> None:
        self.assertFalse(runner.has_channel_errors({"keyword": None, "vector": None}))
        self.assertTrue(runner.has_channel_errors({"vector": "timeout"}))

    def test_fake_is_rejected_as_baseline_but_can_run_smoke(self) -> None:
        smoke_scope = "00000000-0000-4000-8000-000000000001"
        response = {
            "scope_summary": {
                "scope_node_id": smoke_scope,
                "include_descendants": True,
                "scope_snapshot_hash": "sha256:test",
                "scope_path": "root.scope-a",
                "node_kind": "folder",
                "node_title": "Scope A",
                "knowledge_count": 0,
                "source_count": 0,
                "source_version_count": 0,
                "chunk_count": 0,
                "index_status": "ready",
                "index_config_version": "v1",
            },
            "embedding": {"provider": "fake", "model": "fake-embedding", "dimensions": 1024},
            "keyword_hits": [],
            "vector_hits": [],
            "channel_errors": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            dataset = self.write_dataset(directory)
            fixture_map = self.write_fixture_map(directory)
            real_mapping_response = dict(response)
            real_mapping_response["scope_summary"] = dict(response["scope_summary"])
            real_mapping_response["scope_summary"]["scope_node_id"] = "scope-a"
            with (
                patch.object(runner, "post_json", return_value=real_mapping_response),
                self.assertRaisesRegex(RuntimeError, "cannot be reported as a real baseline"),
            ):
                runner.run(self.args(dataset, fixture_map=fixture_map))
            with patch.object(runner, "post_json", return_value=response):
                fake_args = self.args(dataset, allow_fake=True)
                fake_args.smoke_scope_node_id = smoke_scope
                report = runner.run(fake_args)
        self.assertFalse(report["baseline_eligible"])
        self.assertEqual(report["run_kind"], "fake-smoke")

    def test_fake_smoke_requires_a_real_scope_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = self.write_dataset(directory)
            with self.assertRaisesRegex(RuntimeError, "smoke-scope-node-id is required"):
                runner.run(self.args(dataset, allow_fake=True))
            invalid_args = self.args(dataset, allow_fake=True)
            invalid_args.smoke_scope_node_id = "scope-a"
            with self.assertRaisesRegex(ValueError, "must be a UUID"):
                runner.run(invalid_args)


if __name__ == "__main__":
    unittest.main()
