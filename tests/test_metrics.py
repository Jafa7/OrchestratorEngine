from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orchestrator_engine import cli, core
from orchestrator_engine.metrics import contracts
from orchestrator_engine.metrics.adapters import collect_orchestrator_engine
from orchestrator_engine.metrics.advisor import advise, compact_guidance
from orchestrator_engine.metrics.calculations import (
    calculate_metrics,
    latest_logical_records,
)
from orchestrator_engine.metrics.catalog import METRIC_DEFINITIONS
from orchestrator_engine.metrics.reporting import (
    build_report,
    compare_reports,
    markdown_report,
)
from orchestrator_engine.metrics.store import MetricsStore, MetricsStoreError


class MetricsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MetricsStore(self.root)
        self.store.initialize()
        self.source = self.store.register_source(
            name="synthetic", source_type="fixture", capabilities=["execution"]
        )["source"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def observation(
        self,
        record_type: str,
        data: dict[str, object],
        *,
        observation_id: str,
        observed_at: str = "2026-09-08T10:00:00Z",
    ) -> dict[str, object]:
        return contracts.make_observation(
            source_id=self.source["source_id"],
            record_type=record_type,
            data=data,
            observation_id=observation_id,
            observed_at=observed_at,
        )

    def test_ingest_is_idempotent_and_generations_are_replayable(self) -> None:
        first_generation = self.store.current_generation()["generation_digest"]
        item = self.observation(
            "execution_attempt", {"execution_id": "run-1"}, observation_id="one"
        )

        committed = self.store.ingest([item])
        repeated = self.store.ingest([item])

        self.assertEqual(committed["imported"], 1)
        self.assertEqual(repeated["status"], "unchanged")
        self.assertEqual(
            self.store.observations(self.store.generation(first_generation)), []
        )
        self.assertEqual(len(self.store.observations()), 1)

    def test_ingest_uses_recoverable_observation_index_after_initial_build(
        self,
    ) -> None:
        first = self.observation(
            "execution_attempt", {"execution_id": "run-1"}, observation_id="one"
        )
        second = self.observation(
            "execution_attempt", {"execution_id": "run-2"}, observation_id="two"
        )
        self.store.ingest([first])

        with patch.object(
            self.store,
            "_observation_index",
            side_effect=AssertionError("full history fallback used"),
        ):
            result = self.store.ingest([second])

        self.assertEqual(result["imported"], 1)
        self.assertFalse(result["index_rebuilt"])

    def test_missing_observation_index_rebuilds_without_duplicate_ingest(self) -> None:
        item = self.observation(
            "execution_attempt", {"execution_id": "run-1"}, observation_id="one"
        )
        self.store.ingest([item])
        self.store.observation_index_path.unlink()

        result = self.store.ingest([item])

        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["imported"], 0)
        self.assertTrue(result["index_rebuilt"])

    def test_token_metric_requires_usage_capability_and_complete_provenance(
        self,
    ) -> None:
        self.store.ingest(
            [
                self.observation(
                    "execution_attempt",
                    {
                        "execution_id": "run-1",
                        "total_tokens": 42000,
                        "usage_measurement_status": "complete",
                    },
                    observation_id="unqualified-usage",
                )
            ]
        )

        report = build_report(self.store)
        metric = next(
            item for item in report["metrics"] if item["metric_id"] == "MET-007"
        )

        self.assertIsNone(metric["value"])
        self.assertEqual(metric["details"]["unqualified_usage_records"], 1)

    def test_attempt_sequence_must_be_a_positive_integer(self) -> None:
        with self.assertRaisesRegex(
            contracts.MetricsContractError,
            "attempt_sequence must be a positive integer",
        ):
            self.observation(
                "execution_attempt",
                {"execution_id": "run-1", "attempt_sequence": 0},
                observation_id="invalid-sequence",
            )

    def test_generation_hash_corruption_fails_closed(self) -> None:
        generation = self.store.current_generation()["generation_digest"]
        path = self.store.root / "generations" / f"{generation}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["created_at"] = "2026-09-08T12:00:00Z"
        core.atomic_json(path, value)

        with self.assertRaisesRegex(MetricsStoreError, "hash mismatch"):
            self.store.current_generation()
        self.assertEqual(self.store.doctor()["status"], "error")

    def test_observation_digest_index_mismatch_fails_closed(self) -> None:
        self.store.ingest(
            [
                self.observation(
                    "work_item",
                    {"work_item_id": "one"},
                    observation_id="one",
                )
            ]
        )
        generation = self.store.current_generation()
        original_digest = generation["segment_digests"][0]
        path = self.store.root / "segments" / f"{original_digest}.json"
        segment = json.loads(path.read_text(encoding="utf-8"))
        segment["observation_digests"]["one"] = "f" * 64
        changed_digest = contracts.content_digest(segment)
        core.claim_json(
            self.store.root / "segments" / f"{changed_digest}.json", segment
        )

        with self.assertRaisesRegex(MetricsStoreError, "digest index differs"):
            self.store.observations(
                {**generation, "segment_digests": [changed_digest]}
            )

    def test_failed_selector_write_leaves_new_objects_invisible(self) -> None:
        before = self.store.current_generation()["generation_digest"]
        item = self.observation(
            "execution_attempt", {"execution_id": "orphan"}, observation_id="orphan"
        )
        real_atomic_json = core.atomic_json

        def fail_selector(path: Path, value: object) -> None:
            if path == self.store.current_path:
                raise OSError("synthetic crash")
            real_atomic_json(path, value)

        with (
            patch("orchestrator_engine.metrics.store.core.atomic_json", fail_selector),
            self.assertRaisesRegex(OSError, "synthetic crash"),
        ):
            self.store.ingest([item])

        self.assertEqual(self.store.current_generation()["generation_digest"], before)
        self.assertEqual(self.store.observations(), [])

    def test_import_is_bounded_and_requires_registered_source(self) -> None:
        values = [
            self.observation(
                "work_item", {"work_item_id": str(index)}, observation_id=str(index)
            )
            for index in range(contracts.MAX_IMPORT_RECORDS + 1)
        ]
        with self.assertRaisesRegex(MetricsStoreError, "at most"):
            self.store.ingest(values)

        invalid = {**values[0], "source_id": "33333333-3333-4333-8333-333333333333"}
        with self.assertRaisesRegex(MetricsStoreError, "unregistered"):
            self.store.ingest([invalid])

    def test_duplicate_id_requires_identical_content(self) -> None:
        original = self.observation(
            "work_item", {"work_item_id": "one"}, observation_id="stable-id"
        )
        conflicting = {
            **original,
            "data": {"work_item_id": "different"},
        }
        self.store.ingest([original])

        with self.assertRaisesRegex(MetricsStoreError, "different content"):
            self.store.ingest([conflicting])

    def test_conflicting_batch_duplicate_is_rejected_atomically(self) -> None:
        original = self.observation(
            "work_item", {"work_item_id": "one"}, observation_id="batch-id"
        )
        conflicting = {**original, "data": {"work_item_id": "different"}}
        before = self.store.current_generation()["generation_digest"]

        with self.assertRaisesRegex(MetricsStoreError, "more than once"):
            self.store.ingest([original, conflicting])

        self.assertEqual(
            self.store.current_generation()["generation_digest"], before
        )
        self.assertEqual(self.store.observations(), [])

    def test_identical_duplicate_inside_one_batch_is_counted(self) -> None:
        item = self.observation(
            "work_item", {"work_item_id": "one"}, observation_id="batch-id"
        )

        result = self.store.ingest([item, item])

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["duplicates"], 1)

    def test_disabled_source_rejects_collection(self) -> None:
        disabled = self.store.register_source(
            name="disabled", source_type="fixture", enabled=False
        )["source"]
        item = contracts.make_observation(
            source_id=disabled["source_id"],
            record_type="work_item",
            data={"work_item_id": "one"},
            observation_id="disabled-item",
        )

        with self.assertRaisesRegex(MetricsStoreError, "source is disabled"):
            self.store.ingest([item])

    def test_disabling_source_preserves_historical_generation(self) -> None:
        self.store.ingest(
            [
                self.observation(
                    "acceptance",
                    {"work_item_id": "one", "accepted": True},
                    observation_id="accepted",
                )
            ]
        )
        enabled_generation = self.store.current_generation()["generation_digest"]

        changed = self.store.set_source_enabled(
            self.source["source_id"], enabled=False
        )
        current = build_report(
            self.store, evaluation_time="2026-09-08T11:00:00Z"
        )
        historical = build_report(
            self.store,
            generation_digest=enabled_generation,
            evaluation_time="2026-09-08T11:00:00Z",
        )

        self.assertEqual(changed["status"], "updated")
        self.assertEqual(current["observation_count"], 0)
        self.assertEqual(historical["observation_count"], 1)
        with self.assertRaisesRegex(MetricsStoreError, "source is disabled"):
            self.store.ingest(
                [
                    self.observation(
                        "work_item",
                        {"work_item_id": "two"},
                        observation_id="disabled-second-write",
                    )
                ]
            )

    def test_source_inventory_must_match_declared_capabilities(self) -> None:
        with self.assertRaisesRegex(ValueError, "must match"):
            self.store.register_source(
                name="mismatched",
                source_type="fixture",
                capabilities=["execution"],
                capability_inventory=[],
            )

    def test_report_pins_generation_and_json_matches_markdown(self) -> None:
        self.store.ingest(
            [
                self.observation(
                    "acceptance",
                    {"work_item_id": "work-1", "accepted": True},
                    observation_id="accept-1",
                )
            ]
        )
        report = build_report(self.store)
        markdown = markdown_report(report)

        self.assertEqual(report["kind"], contracts.REPORT_KIND)
        self.assertIn(report["generation_digest"], markdown)
        self.assertIn("MET-001 Accepted outcomes | 1", markdown)

    def test_concurrent_imports_serialize_without_lost_observations(self) -> None:
        values = [
            self.observation(
                "execution_attempt",
                {"execution_id": f"run-{index}"},
                observation_id=f"run-{index}",
            )
            for index in range(8)
        ]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda item: self.store.ingest([item]), values))

        self.assertEqual(len(self.store.observations()), 8)
        self.assertEqual(self.store.doctor()["status"], "ok")

    def test_explicit_recovery_repairs_a_broken_selector(self) -> None:
        expected = self.store.current_generation()["generation_digest"]
        core.atomic_json(
            self.store.current_path,
            {
                "schema_version": 1,
                "kind": "ORCHESTRATOR_METRICS_CURRENT",
                "generation_digest": "f" * 64,
                "updated_at": "2026-09-08T10:00:00Z",
            },
        )

        recovered = self.store.recover()

        self.assertEqual(recovered["generation"], expected)
        self.assertEqual(self.store.doctor()["status"], "ok")

    def test_explicit_recovery_restores_a_missing_selector(self) -> None:
        expected = self.store.current_generation()["generation_digest"]
        self.store.current_path.unlink()

        recovered = self.store.recover()

        self.assertEqual(recovered["generation"], expected)
        self.assertEqual(self.store.doctor()["status"], "ok")

    def test_runtime_rejects_unsupported_generation_schema(self) -> None:
        generation = self.store.current_generation()["generation_digest"]
        path = self.store.root / "generations" / f"{generation}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        body = {key: item for key, item in value.items() if key != "generation_digest"}
        body["schema_version"] = 2
        digest = contracts.content_digest(body)
        core.claim_json(
            self.store.root / "generations" / f"{digest}.json",
            {**body, "generation_digest": digest},
        )

        with self.assertRaisesRegex(MetricsStoreError, "unsupported"):
            self.store.generation(digest)

    def test_report_applies_effective_and_knowledge_cutoffs(self) -> None:
        late_known = contracts.make_observation(
            source_id=self.source["source_id"],
            record_type="acceptance",
            data={"work_item_id": "one", "accepted": True},
            observation_id="late-known",
            observed_at="2026-09-08T10:00:00Z",
            effective_at="2026-09-08T09:00:00Z",
            known_at="2026-09-08T12:00:00Z",
        )
        self.store.ingest([late_known])

        earlier = build_report(self.store, evaluation_time="2026-09-08T11:00:00Z")
        later = build_report(self.store, evaluation_time="2026-09-08T13:00:00Z")

        self.assertEqual(earlier["observation_count"], 0)
        self.assertEqual(later["observation_count"], 1)

    def test_report_rejects_a_timezone_free_evaluation_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            build_report(self.store, evaluation_time="2026-09-08T11:00:00")

    def test_compare_rejects_different_cohorts(self) -> None:
        evaluation_time = "2026-09-08T13:00:00Z"
        first = build_report(
            self.store,
            package_id="package-a",
            evaluation_time=evaluation_time,
        )
        second = build_report(
            self.store,
            package_id="package-b",
            evaluation_time=evaluation_time,
        )

        result = compare_reports(first, second)

        self.assertEqual(result["status"], "incompatible")
        self.assertEqual(result["incompatible_fields"], ["cohort"])
        self.assertTrue(all(not item["comparable"] for item in result["metrics"]))

    def test_compare_requires_the_same_evaluation_cutoff(self) -> None:
        first = build_report(self.store, evaluation_time="2026-09-08T13:00:00Z")
        second = build_report(self.store, evaluation_time="2026-09-08T14:00:00Z")

        result = compare_reports(first, second)

        self.assertEqual(result["status"], "incompatible")
        self.assertIn("evaluation_time", result["incompatible_fields"])


class MetricsCalculationTests(unittest.TestCase):
    SOURCE = "11111111-1111-4111-8111-111111111111"

    def item(
        self,
        record_type: str,
        data: dict[str, object],
        observation_id: str,
        observed_at: str = "2026-09-08T10:00:00Z",
    ) -> dict[str, object]:
        return contracts.make_observation(
            source_id=self.SOURCE,
            record_type=record_type,
            data=data,
            observation_id=observation_id,
            observed_at=observed_at,
        )

    def metrics(self, values: list[dict[str, object]]) -> dict[str, dict[str, object]]:
        return {item["metric_id"]: item for item in calculate_metrics(values)}

    def test_catalog_distinguishes_calculated_views_and_external_fields(self) -> None:
        self.assertEqual(len(METRIC_DEFINITIONS), 11)
        for definition in METRIC_DEFINITIONS:
            with self.subTest(metric_id=definition["metric_id"]):
                self.assertTrue(definition["calculated_fields"])
                self.assertTrue(definition["evidence_view_fields"])
                self.assertTrue(definition["external_integration_fields"])
                self.assertEqual(
                    definition["producer_status"], "explicit_source_required"
                )

    def test_parallel_and_nested_intervals_use_union_not_sum(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": "parent",
                    "started_at": "2026-09-08T10:00:00Z",
                    "finished_at": "2026-09-08T10:01:00Z",
                },
                "parent",
            ),
            self.item(
                "execution_attempt",
                {
                    "execution_id": "child",
                    "started_at": "2026-09-08T10:00:10Z",
                    "finished_at": "2026-09-08T10:00:50Z",
                },
                "child",
            ),
        ]
        result = self.metrics(values)["MET-002"]
        self.assertEqual(result["value"], 60)
        self.assertEqual(result["details"]["root_execution_sum_seconds"], 100)

    def test_late_known_correction_supersedes_by_knowledge_time(self) -> None:
        original = self.item(
            "classification",
            {"classification_id": "state-1", "status": "old"},
            "original",
            "2026-09-08T11:00:00Z",
        )
        correction = contracts.make_observation(
            source_id=self.SOURCE,
            record_type="classification",
            data={"classification_id": "state-1", "status": "corrected"},
            observation_id="correction",
            observed_at="2026-09-08T10:00:00Z",
            known_at="2026-09-08T12:00:00Z",
        )

        records = latest_logical_records([original, correction])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["data"]["status"], "corrected")

    def test_latest_record_uses_absolute_time_not_timestamp_text(self) -> None:
        earlier = contracts.make_observation(
            source_id=self.SOURCE,
            record_type="classification",
            data={"classification_id": "state-1", "status": "earlier"},
            observation_id="earlier",
            observed_at="2026-09-08T10:00:00+02:00",
        )
        later = contracts.make_observation(
            source_id=self.SOURCE,
            record_type="classification",
            data={"classification_id": "state-1", "status": "later"},
            observation_id="later",
            observed_at="2026-09-08T09:30:00Z",
        )

        records = latest_logical_records([later, earlier])

        self.assertEqual(records[0]["data"]["status"], "later")

    def test_nested_child_time_is_a_breakdown_not_added_to_root_execution(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": "parent",
                    "started_at": "2026-09-08T10:00:00Z",
                    "finished_at": "2026-09-08T10:01:00Z",
                },
                "parent",
            ),
            self.item(
                "execution_attempt",
                {
                    "execution_id": "child",
                    "parent_execution_id": "parent",
                    "started_at": "2026-09-08T10:00:10Z",
                    "finished_at": "2026-09-08T10:00:50Z",
                },
                "child",
            ),
        ]

        details = self.metrics(values)["MET-002"]["details"]

        self.assertEqual(details["root_execution_sum_seconds"], 60)
        self.assertEqual(details["nested_breakdown_seconds"], 40)

    def test_mirrored_execution_and_usage_event_are_counted_once(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": "run",
                    "usage_event_id": "usage",
                    "total_tokens": 100,
                    "usage_measurement_status": "complete",
                },
                "mirror-a",
                "2026-09-08T10:00:00Z",
            ),
            self.item(
                "execution_attempt",
                {
                    "execution_id": "run",
                    "usage_event_id": "usage",
                    "total_tokens": 100,
                    "usage_measurement_status": "complete",
                },
                "mirror-b",
                "2026-09-08T10:01:00Z",
            ),
        ]
        metrics = self.metrics(values)
        self.assertEqual(metrics["MET-007"]["value"], 100)
        self.assertEqual(metrics["MET-009"]["coverage"]["total"], 1)

    def test_missing_usage_is_unknown_not_zero(self) -> None:
        metrics = self.metrics(
            [self.item("execution_attempt", {"execution_id": "run"}, "run")]
        )
        self.assertIsNone(metrics["MET-007"]["value"])
        self.assertEqual(metrics["MET-007"]["availability"], "unavailable")
        self.assertEqual(metrics["MET-007"]["value_state"], "unknown")
        self.assertEqual(metrics["MET-009"]["value"], 0)
        self.assertEqual(metrics["MET-009"]["coverage"]["status"], "known")
        self.assertEqual(metrics["MET-009"]["coverage"]["observed"], 1)
        self.assertEqual(metrics["MET-009"]["details"]["known_usage_attempts"], 0)

    def test_partial_usage_is_not_promoted_to_a_complete_total(self) -> None:
        metrics = self.metrics(
            [
                self.item(
                    "execution_attempt",
                    {
                        "execution_id": "run",
                        "total_tokens": 310000,
                        "usage_measurement_status": "partial",
                    },
                    "run",
                )
            ]
        )
        self.assertIsNone(metrics["MET-007"]["value"])
        self.assertEqual(metrics["MET-007"]["value_state"], "partial")
        self.assertEqual(metrics["MET-007"]["coverage"]["status"], "partial")
        self.assertEqual(
            metrics["MET-007"]["details"]["partial_reported_tokens_lower_bound"],
            310000,
        )

    def test_mirrored_partial_usage_is_deduplicated(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": f"mirror-{index}",
                    "usage_event_id": "shared-usage",
                    "total_tokens": 310000,
                    "usage_measurement_status": "partial",
                },
                f"mirror-{index}",
            )
            for index in range(2)
        ]

        result = self.metrics(values)["MET-007"]

        self.assertEqual(result["details"]["partial_usage_records"], 1)
        self.assertEqual(
            result["details"]["partial_reported_tokens_lower_bound"], 310000
        )

    def test_same_usage_event_from_independent_sources_is_not_collapsed(self) -> None:
        other_source = "22222222-2222-4222-8222-222222222222"
        values = [
            contracts.make_observation(
                source_id=source,
                record_type="execution_attempt",
                data={
                    "execution_id": "run",
                    "usage_event_id": "usage",
                    "total_tokens": 100,
                    "usage_measurement_status": "complete",
                },
                observation_id=f"usage-{index}",
                observed_at="2026-09-08T10:00:00Z",
            )
            for index, source in enumerate((self.SOURCE, other_source))
        ]

        result = self.metrics(values)["MET-007"]

        self.assertEqual(result["value"], 200)
        self.assertEqual(result["details"]["deduplicated_usage_events"], 2)

    def test_cost_buckets_reconcile_without_hiding_shared_cost(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": bucket,
                    "cost_units": cost,
                    "cost_bucket": bucket,
                },
                bucket,
            )
            for bucket, cost in (("accepted", 8), ("unfinished", 7), ("shared", 5))
        ]
        details = self.metrics(values)["MET-007"]["details"]
        self.assertEqual(details["observed_cost_units"], 20)
        self.assertEqual(details["accepted_cost_units"], 8)
        self.assertEqual(details["shared_or_unallocated_cost_units"], 5)
        self.assertEqual(details["cost_conservation"], "balanced")

    def test_shared_cost_identity_is_source_scoped(self) -> None:
        other_source = "22222222-2222-4222-8222-222222222222"
        values = [
            contracts.make_observation(
                source_id=source,
                record_type="execution_attempt",
                data={
                    "execution_id": f"run-{index}",
                    "shared_cost_id": "shared",
                    "cost_units": 5,
                },
                observation_id=f"cost-{index}",
                observed_at="2026-09-08T10:00:00Z",
            )
            for index, source in enumerate((self.SOURCE, other_source))
        ]

        details = self.metrics(values)["MET-007"]["details"]

        self.assertEqual(details["observed_cost_units"], 10)

    def test_parent_execution_identity_is_source_scoped(self) -> None:
        other_source = "22222222-2222-4222-8222-222222222222"
        values = [
            contracts.make_observation(
                source_id=self.SOURCE,
                record_type="execution_attempt",
                data={
                    "execution_id": "parent",
                    "started_at": "2026-09-08T10:00:00Z",
                    "finished_at": "2026-09-08T10:01:00Z",
                },
                observation_id="parent-a",
                observed_at="2026-09-08T10:01:00Z",
            ),
            contracts.make_observation(
                source_id=other_source,
                record_type="execution_attempt",
                data={
                    "execution_id": "child",
                    "parent_execution_id": "parent",
                    "started_at": "2026-09-08T10:00:10Z",
                    "finished_at": "2026-09-08T10:00:50Z",
                },
                observation_id="child-b",
                observed_at="2026-09-08T10:01:00Z",
            ),
        ]

        details = self.metrics(values)["MET-002"]["details"]

        self.assertEqual(details["root_execution_sum_seconds"], 100)
        self.assertEqual(details["nested_breakdown_seconds"], 0)

    def test_quota_keeps_account_reset_and_rounding_semantics(self) -> None:
        metrics = self.metrics(
            [
                self.item(
                    "quota_sample",
                    {
                        "account_alias": "shared-account",
                        "remaining_percentage_points": 62.5,
                        "reset_at": "2026-09-09T00:00:00Z",
                        "rounding": "nearest_half_point",
                    },
                    "quota",
                )
            ]
        )
        value = metrics["MET-008"]["value"]["shared-account"]
        self.assertEqual(value["remaining_percentage_points"], 62.5)
        self.assertEqual(value["rounding"], "nearest_half_point")

    def test_first_gate_keeps_pending_and_not_started_in_denominator(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": status,
                    "first_gate_expected": True,
                    "first_gate_status": status,
                },
                status,
            )
            for status in ("passed", "failed", "pending", "not_started")
        ]
        result = self.metrics(values)["MET-003"]
        self.assertEqual(result["value"], 0.25)
        self.assertEqual(result["details"]["expected_not_started"], 1)


class MetricsAdvisorTests(unittest.TestCase):
    SOURCE = "11111111-1111-4111-8111-111111111111"

    def item(self, record_type: str, data: dict[str, object], name: str):
        return contracts.make_observation(
            source_id=self.SOURCE,
            record_type=record_type,
            data=data,
            observation_id=name,
            observed_at="2026-09-08T10:00:00Z",
        )

    def test_package_scope_requires_binding(self) -> None:
        result = advise([], scope="package")
        self.assertEqual(result["reason"], "package_context_unavailable")
        self.assertEqual(result["authority"], "advisory_only")

    def test_running_operation_is_not_duplicated(self) -> None:
        result = advise(
            [
                self.item(
                    "execution_attempt",
                    {"operation_id": "run", "status": "running"},
                    "running",
                )
            ],
            scope="operation_only",
            operation_id="run",
        )
        self.assertEqual(result["next_action"], "wait_for_running_operation")
        self.assertFalse(result["details"]["check_admissible_now"])

    def test_repetition_recommends_diagnosis_but_useful_delta_does_not(self) -> None:
        repeated = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": f"run-{index}",
                    "equivalence_key": "same-purpose-and-inputs",
                    "evidence_delta": False,
                    "status": "completed",
                },
                f"run-{index}",
            )
            for index in range(2)
        ]
        self.assertEqual(
            advise(repeated, scope="operation_only")["next_action"],
            "diagnose_before_repeating",
        )
        repeated[-1]["data"]["evidence_delta"] = True
        self.assertNotEqual(
            advise(repeated, scope="operation_only")["next_action"],
            "diagnose_before_repeating",
        )

    def test_final_gate_requires_explicit_project_evidence(self) -> None:
        result = advise(
            [
                self.item(
                    "execution_attempt",
                    {
                        "execution_id": "gate",
                        "verification_role": "final_gate",
                        "status": "completed",
                        "accepted": True,
                        "authority_status": "owner_authorized",
                        "candidate_id": "candidate-1",
                        "check_plan_revision": "plan-1",
                        "evidence_digest": "a" * 64,
                    },
                    "gate",
                )
            ],
            scope="operation_only",
        )
        self.assertEqual(result["next_action"], "handoff_operation_result")
        self.assertFalse(result["details"]["publication_eligible"])
        self.assertEqual(len(compact_guidance(result).splitlines()), 11)
        self.assertEqual(len(result["guidance_snapshot_id"]), 64)

    def test_worker_claim_cannot_authorize_its_own_final_gate(self) -> None:
        result = advise(
            [
                self.item(
                    "execution_attempt",
                    {
                        "execution_id": "gate",
                        "verification_role": "final_gate",
                        "status": "completed",
                        "accepted": True,
                        "authority_status": "worker_claimed",
                        "candidate_id": "candidate-1",
                        "check_plan_revision": "plan-1",
                        "evidence_digest": "a" * 64,
                    },
                    "gate",
                )
            ],
            scope="operation_only",
        )

        self.assertEqual(result["next_action"], "handoff_operation_result")
        self.assertFalse(result["details"]["publication_eligible"])

    def test_incomplete_or_invalidated_reuse_requires_refresh(self) -> None:
        reuse = self.item(
            "classification",
            {
                "classification_kind": "reuse_decision",
                "status": "eligible",
                "requirement_id": "REQ-1",
            },
            "reuse",
        )
        result = advise([reuse], scope="operation_only")
        self.assertEqual(result["next_action"], "refresh_invalidated_evidence")
        self.assertEqual(result["details"]["invalid_reuse_decisions"], 1)

    def test_foreign_mutation_does_not_erase_historical_proof(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": "gate",
                    "verification_role": "final_gate",
                    "status": "completed",
                    "accepted": True,
                    "authority_status": "owner_authorized",
                    "candidate_id": "candidate-1",
                    "check_plan_revision": "plan-1",
                    "evidence_digest": "a" * 64,
                },
                "gate",
            ),
            self.item(
                "classification",
                {
                    "classification_kind": "environment_mutation",
                    "planned": False,
                },
                "mutation",
            ),
        ]
        result = advise(
            values,
            scope="operation_only",
            project_owner_source_ids={self.SOURCE},
        )
        self.assertEqual(result["next_action"], "restore_or_revalidate_environment")
        self.assertTrue(result["details"]["historical_verification"])
        self.assertFalse(result["details"]["publication_eligible"])

    def test_planned_mutation_preserves_history_but_requires_revalidation(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": "gate",
                    "verification_role": "final_gate",
                    "status": "completed",
                    "accepted": True,
                    "authority_status": "owner_authorized",
                    "candidate_id": "candidate-1",
                    "check_plan_revision": "plan-1",
                    "evidence_digest": "a" * 64,
                },
                "gate",
            ),
            self.item(
                "classification",
                {
                    "classification_kind": "environment_mutation",
                    "planned": True,
                    "invalidates_live_readiness": True,
                },
                "mutation",
            ),
        ]
        result = advise(
            values,
            scope="operation_only",
            project_owner_source_ids={self.SOURCE},
        )
        self.assertEqual(result["next_action"], "revalidate_environment")
        self.assertTrue(result["details"]["historical_verification"])
        self.assertFalse(result["details"]["live_readiness"])

    def test_package_scope_requires_complete_project_binding(self) -> None:
        incomplete = self.item(
            "classification",
            {
                "classification_kind": "package_binding",
                "package_id": "package-1",
                "candidate_id": "candidate-1",
            },
            "binding",
        )
        result = advise(
            [incomplete],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.SOURCE},
        )
        self.assertEqual(result["reason"], "package_binding_incomplete")

    def test_package_scope_rejects_final_gate_for_an_older_candidate(self) -> None:
        binding = self.item(
            "classification",
            {
                "classification_kind": "package_binding",
                "classification_id": "package-binding",
                "package_id": "package-1",
                "requirement_set_id": "requirements-1",
                "obligation_ids": [],
                "obligations_complete": True,
                "candidate_id": "candidate-2",
                "check_plan_revision": "plan-2",
                "policy_revision": "policy-1",
            },
            "binding",
        )
        stale_gate = self.item(
            "execution_attempt",
            {
                "execution_id": "gate-1",
                "package_id": "package-1",
                "verification_role": "final_gate",
                "status": "completed",
                "accepted": True,
                "authority_status": "owner_authorized",
                "candidate_id": "candidate-1",
                "check_plan_revision": "plan-1",
                "policy_revision": "policy-1",
                "evidence_digest": "a" * 64,
            },
            "gate",
        )

        result = advise(
            [binding, stale_gate],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.SOURCE},
        )

        self.assertEqual(result["next_action"], "run_project_required_final_gate")
        self.assertFalse(result["details"]["publication_eligible"])

    def test_reuse_requires_current_binding_and_source_execution(self) -> None:
        binding = self.item(
            "classification",
            {
                "classification_kind": "package_binding",
                "classification_id": "package-binding",
                "package_id": "package-1",
                "requirement_set_id": "requirements-1",
                "requirement_ids": ["REQ-1"],
                "obligation_ids": [],
                "obligations_complete": True,
                "candidate_id": "candidate-2",
                "check_plan_revision": "plan-2",
                "policy_revision": "policy-1",
            },
            "binding",
        )
        execution = self.item(
            "execution_attempt",
            {
                "execution_id": "source-check",
                "package_id": "package-1",
                "status": "completed",
            },
            "source-check",
        )
        valid_reuse = self.item(
            "classification",
            {
                "classification_kind": "reuse_decision",
                "classification_id": "reuse-1",
                "package_id": "package-1",
                "status": "eligible",
                "requirement_id": "REQ-1",
                "candidate_id": "candidate-2",
                "check_plan_revision": "plan-2",
                "policy_revision": "policy-1",
                "source_execution_id": "source-check",
                "environment_id": "environment-1",
                "evidence_digest": "b" * 64,
            },
            "reuse",
        )

        valid = advise(
            [binding, execution, valid_reuse],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.SOURCE},
        )
        missing_execution = advise(
            [binding, valid_reuse],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.SOURCE},
        )
        stale = advise(
            [
                binding,
                execution,
                {
                    **valid_reuse,
                    "observation_id": "stale-reuse",
                    "data": {**valid_reuse["data"], "candidate_id": "candidate-1"},
                },
            ],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.SOURCE},
        )

        self.assertEqual(valid["next_action"], "run_project_required_final_gate")
        self.assertEqual(
            missing_execution["next_action"], "refresh_invalidated_evidence"
        )
        self.assertEqual(stale["next_action"], "refresh_invalidated_evidence")


class MetricsAdapterAndCliTests(unittest.TestCase):
    SOURCE = "11111111-1111-4111-8111-111111111111"

    def test_normal_cli_path_does_not_import_or_create_metrics_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = (
                "import sys; "
                "from orchestrator_engine import cli; "
                f"cli.main(['--project-root', {temporary!r}, 'runtime-capabilities']); "
                "print('orchestrator_engine.metrics' in sys.modules)"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.stdout.splitlines()[-1], "False")
            self.assertFalse(Path(temporary, ".orchestrator", "metrics").exists())

    def test_adapter_reads_worker_result_without_mutating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".orchestrator" / "tasks" / "task-1" / "result.json"
            value = {
                "schema_version": 1,
                "kind": "WORKER_RESULT",
                "task_id": "task-1",
                "terminal_status": "completed",
                "started_at": "2026-09-08T10:00:00Z",
                "finished_at": "2026-09-08T10:00:01Z",
            }
            core.atomic_json(path, value)
            before = path.read_bytes()

            result = collect_orchestrator_engine(
                root, source_id="11111111-1111-4111-8111-111111111111"
            )

            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["records"][0]["record_type"], "execution_attempt")
            self.assertEqual(path.read_bytes(), before)

    def test_adapter_collects_workstream_lifecycle_as_a_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = (
                root
                / ".orchestrator"
                / "workstreams"
                / "stream-1"
                / "workstream.json"
            )
            core.atomic_json(
                path,
                {
                    "schema_version": 1,
                    "kind": "ORCHESTRATOR_WORKSTREAM",
                    "workstream_id": "stream-1",
                    "status": "waiting_external",
                    "created_at": "2026-09-08T10:00:00Z",
                    "updated_at": "2026-09-08T10:01:00Z",
                    "waiting_on": "synthetic-check",
                },
            )

            result = collect_orchestrator_engine(
                root, source_id="11111111-1111-4111-8111-111111111111"
            )

            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["records"][0]["record_type"], "work_item")
            self.assertEqual(
                result["records"][0]["data"]["status"], "waiting_external"
            )

    def test_adapter_emits_new_snapshot_after_workstream_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = (
                root
                / ".orchestrator"
                / "workstreams"
                / "stream-1"
                / "workstream.json"
            )
            initial = {
                "schema_version": 1,
                "kind": "ORCHESTRATOR_WORKSTREAM",
                "workstream_id": "stream-1",
                "status": "active",
                "created_at": "2026-09-08T10:00:00Z",
                "updated_at": "2026-09-08T10:00:00Z",
            }
            core.atomic_json(path, initial)
            first = collect_orchestrator_engine(root, source_id=self.SOURCE)
            repeated = collect_orchestrator_engine(root, source_id=self.SOURCE)
            core.atomic_json(
                path,
                {
                    **initial,
                    "status": "completed",
                    "updated_at": "2026-09-08T10:01:00Z",
                },
            )
            changed = collect_orchestrator_engine(root, source_id=self.SOURCE)

            self.assertEqual(
                first["records"][0]["observation_id"],
                repeated["records"][0]["observation_id"],
            )
            self.assertNotEqual(
                first["records"][0]["observation_id"],
                changed["records"][0]["observation_id"],
            )

    def test_adapter_recovers_artifact_after_candidate_journal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            warmed = collect_orchestrator_engine(root, source_id=self.SOURCE)
            self.assertEqual(warmed["candidate_count"], 0)
            result_path = (
                root / ".orchestrator" / "tasks" / "T-RECOVERED" / "result.json"
            )
            with patch(
                "orchestrator_engine.platform_runtime.exclusive_file_lock",
                side_effect=RuntimeError("journal unavailable"),
            ):
                core.atomic_json(
                    result_path,
                    {
                        "schema_version": 1,
                        "kind": "WORKER_RESULT",
                        "task_id": "T-RECOVERED",
                        "terminal_status": "failed",
                        "finished_at": "2026-09-08T10:00:00Z",
                    },
                )

            recovered = collect_orchestrator_engine(
                root, source_id=self.SOURCE
            )

        self.assertEqual(recovered["candidate_count"], 1)
        self.assertEqual(recovered["record_count"], 1)
        self.assertTrue(recovered["discovery"]["recovery_rebuilt"])
        self.assertEqual(recovered["discovery"]["pending_recovery_markers"], 0)

    def test_adapter_cursor_eventually_visits_later_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(3):
                core.atomic_json(
                    root
                    / ".orchestrator"
                    / "workstreams"
                    / f"stream-{index}"
                    / "workstream.json",
                    {
                        "schema_version": 1,
                        "kind": "ORCHESTRATOR_WORKSTREAM",
                        "workstream_id": f"stream-{index}",
                        "status": "active",
                        "created_at": "2026-09-08T10:00:00Z",
                    },
                )

            first = collect_orchestrator_engine(
                root, source_id=self.SOURCE, maximum=2
            )
            second = collect_orchestrator_engine(
                root,
                source_id=self.SOURCE,
                maximum=2,
                cursor=first["next_cursor"],
            )

            self.assertTrue(first["truncated"])
            self.assertEqual(first["record_count"], 2)
            self.assertEqual(second["record_count"], 1)
            self.assertTrue(second["next_cursor"].startswith("metrics-index:"))
            third = collect_orchestrator_engine(
                root,
                source_id=self.SOURCE,
                maximum=2,
                cursor=second["next_cursor"],
            )
            self.assertEqual(third["candidate_count"], 0)
            self.assertFalse(third["discovery"]["index_rebuilt"])
            self.assertEqual(third["discovery"]["rebuild_paths_examined"], 0)

    def test_adapter_cursor_skips_deleted_rows_without_losing_later_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []
            for index in range(4):
                path = (
                    root
                    / ".orchestrator"
                    / "workstreams"
                    / f"stream-{index}"
                    / "workstream.json"
                )
                paths.append(path)
                core.atomic_json(
                    path,
                    {
                        "schema_version": 1,
                        "kind": "ORCHESTRATOR_WORKSTREAM",
                        "workstream_id": f"stream-{index}",
                        "status": "active",
                        "created_at": "2026-09-08T10:00:00Z",
                    },
                )

            first = collect_orchestrator_engine(
                root, source_id=self.SOURCE, maximum=1
            )
            paths[1].unlink()
            paths[2].unlink()
            second = collect_orchestrator_engine(
                root,
                source_id=self.SOURCE,
                maximum=1,
                cursor=first["next_cursor"],
            )

            self.assertEqual(second["record_count"], 1)
            self.assertEqual(
                second["records"][0]["data"]["work_item_id"], "stream-3"
            )
            self.assertGreaterEqual(
                second["discovery"]["candidate_rows_examined"], 3
            )

    def test_adapter_keeps_unavailable_usage_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".orchestrator" / "tasks" / "task-1" / "usage.json"
            core.atomic_json(
                path,
                {
                    "schema_version": 1,
                    "kind": "WORKER_USAGE",
                    "task_id": "task-1",
                    "worker": "synthetic",
                    "adapter": "codex-jsonl-usage",
                    "measurement_status": "unavailable",
                    "total_tokens": 0,
                    "captured_at": "2026-09-08T10:00:00Z",
                },
            )

            result = collect_orchestrator_engine(
                root, source_id="11111111-1111-4111-8111-111111111111"
            )

            data = result["records"][0]["data"]
            self.assertEqual(data["usage_measurement_status"], "unavailable")
            self.assertNotIn("total_tokens", data)

    def test_adapter_recognizes_worker_and_followup_terminal_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / ".orchestrator" / "events"
            for event_id, kind in (
                ("worker-event", "WORKER_TERMINAL"),
                ("check-event", "ORCHESTRATOR_TERMINAL"),
            ):
                core.atomic_json(
                    events / f"{event_id}.json",
                    {
                        "schema_version": 1,
                        "kind": kind,
                        "event_id": event_id,
                        "task_id": event_id,
                        "created_at": "2026-09-08T10:00:00Z",
                    },
                )

            result = collect_orchestrator_engine(
                root, source_id="11111111-1111-4111-8111-111111111111"
            )

            self.assertEqual(result["record_count"], 2)
            self.assertEqual(
                {item["data"]["event_id"] for item in result["records"]},
                {"worker-event", "check-event"},
            )

    def test_adapter_recognizes_manual_delivery_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = (
                root
                / ".orchestrator"
                / "inbox"
                / "acknowledgements"
                / "event-1.json"
            )
            core.atomic_json(
                path,
                {
                    "schema_version": 1,
                    "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_ACKNOWLEDGEMENT",
                    "event_id": "event-1",
                    "status": "acknowledged",
                    "acknowledged_at": "2026-09-08T10:00:00Z",
                },
            )

            result = collect_orchestrator_engine(
                root, source_id="11111111-1111-4111-8111-111111111111"
            )

            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["records"][0]["data"]["status"], "acknowledged")

    def test_adapter_merges_complete_usage_with_worker_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / ".orchestrator" / "tasks" / "task-1"
            core.atomic_json(
                task / "result.json",
                {
                    "schema_version": 1,
                    "kind": "WORKER_RESULT",
                    "task_id": "task-1",
                    "terminal_status": "completed",
                    "started_at": "2026-09-08T10:00:00Z",
                    "finished_at": "2026-09-08T10:00:01Z",
                },
            )
            core.atomic_json(
                task / "usage.json",
                {
                    "schema_version": 1,
                    "kind": "WORKER_USAGE",
                    "task_id": "task-1",
                    "worker": "synthetic",
                    "adapter": "codex-jsonl-usage",
                    "measurement_status": "complete",
                    "total_tokens": 42,
                    "captured_at": "2026-09-08T10:00:02Z",
                },
            )

            result = collect_orchestrator_engine(
                root, source_id="11111111-1111-4111-8111-111111111111"
            )
            metrics = {
                item["metric_id"]: item for item in calculate_metrics(result["records"])
            }

            self.assertEqual(metrics["MET-007"]["value"], 42)
            self.assertEqual(metrics["MET-009"]["coverage"]["total"], 1)

    def test_adapter_keeps_worker_and_check_with_same_native_id_distinct(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / ".orchestrator" / "tasks" / "SAME"
            check = root / ".orchestrator" / "checks" / "SAME"
            core.atomic_json(
                task / "result.json",
                {
                    "kind": "WORKER_RESULT",
                    "task_id": "SAME",
                    "terminal_status": "completed",
                    "started_at": "2026-09-08T10:00:00Z",
                    "finished_at": "2026-09-08T10:00:10Z",
                    "duration_seconds": 10,
                },
            )
            core.atomic_json(
                task / "usage.json",
                {
                    "kind": "WORKER_USAGE",
                    "task_id": "SAME",
                    "measurement_status": "complete",
                    "total_tokens": 15,
                    "captured_at": "2026-09-08T10:00:11Z",
                },
            )
            core.atomic_json(
                check / "verification-result.json",
                {
                    "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
                    "check_id": "SAME",
                    "status": "passed",
                    "started_at": "2026-09-08T10:01:00Z",
                    "finished_at": "2026-09-08T10:01:03Z",
                    "duration_seconds": 3,
                },
            )
            store = MetricsStore(root)
            store.initialize()
            store.register_source(
                name="runtime",
                source_type="orchestrator_engine",
                source_id=self.SOURCE,
                capabilities=["execution", "usage"],
            )
            collected = collect_orchestrator_engine(root, source_id=self.SOURCE)
            store.ingest(collected["records"])

            report = build_report(store)
            metrics = {item["metric_id"]: item for item in report["metrics"]}

            self.assertEqual(metrics["MET-002"]["value"], 13)
            self.assertEqual(metrics["MET-007"]["value"], 15)

    def test_adapter_links_delivery_to_operation_across_cursor_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event = root / ".orchestrator" / "events" / "event-1.json"
            receipt = (
                root
                / ".orchestrator"
                / "inbox"
                / "thread-wakeups"
                / "event-1.json"
            )
            core.atomic_json(
                event,
                {
                    "kind": "WORKER_TERMINAL",
                    "event_id": "event-1",
                    "task_id": "TASK-1",
                    "created_at": "2026-09-08T10:00:00Z",
                },
            )
            core.atomic_json(
                receipt,
                {
                    "kind": "CURRENT_THREAD_WAKEUP",
                    "event_id": "event-1",
                    "status": "woken",
                    "created_at": "2026-09-08T10:01:00Z",
                },
            )
            first = collect_orchestrator_engine(
                root,
                source_id=self.SOURCE,
                maximum=1,
            )
            second = collect_orchestrator_engine(
                root,
                source_id=self.SOURCE,
                maximum=1,
                cursor=first["next_cursor"],
            )
            self.assertEqual(second["records"][0]["data"]["operation_id"], "TASK-1")

            store = MetricsStore(root)
            store.initialize()
            store.register_source(
                name="runtime",
                source_type="orchestrator_engine",
                source_id=self.SOURCE,
                capabilities=["delivery"],
            )
            store.ingest([*first["records"], *second["records"]])

            report = build_report(store, operation_id="TASK-1")
            metric = next(
                item for item in report["metrics"] if item["metric_id"] == "MET-010"
            )

            self.assertEqual(report["observation_count"], 1)
            self.assertEqual(metric["value"], 1)
            self.assertEqual(metric["details"]["delivered"], 1)

            acknowledgement = (
                root
                / ".orchestrator"
                / "inbox"
                / "acknowledgements"
                / "event-1.json"
            )
            core.atomic_json(
                acknowledgement,
                {
                    "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_ACKNOWLEDGEMENT",
                    "event_id": "event-1",
                    "status": "acknowledged",
                    "acknowledged_at": "2026-09-08T10:02:00Z",
                },
            )
            acknowledgement_page = collect_orchestrator_engine(
                root,
                source_id=self.SOURCE,
                maximum=1,
                cursor=str(event),
            )
            self.assertEqual(
                acknowledgement_page["records"][0]["data"]["operation_id"],
                "TASK-1",
            )
            store.ingest(acknowledgement_page["records"])
            acknowledged_report = build_report(store, operation_id="TASK-1")
            acknowledged_metric = next(
                item
                for item in acknowledged_report["metrics"]
                if item["metric_id"] == "MET-010"
            )
            self.assertEqual(acknowledged_metric["value"], 1)

    def test_cli_is_opt_in_and_runs_lazily(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--project-root", temporary, "metrics", "init"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "initialized")

    def test_cli_registers_explicit_source_capability_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "capabilities.json"
            inventory.write_text(
                json.dumps(
                    [
                        {
                            "name": "usage",
                            "status": "supported",
                            "units": ["tokens"],
                            "counter_semantics": "provider_total",
                            "precision": "exact",
                            "clock": "provider",
                            "freshness": "completion",
                            "permissions": "local_read",
                            "overhead": "bounded",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main(["--project-root", temporary, "metrics", "init"]), 0
                )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        temporary,
                        "metrics",
                        "sources",
                        "register",
                        "--name",
                        "provider-usage",
                        "--type",
                        "provider-export",
                        "--adapter-version",
                        "2",
                        "--authority",
                        "provider_reported",
                        "--observation-semantics",
                        "immutable_events",
                        "--capability",
                        "usage",
                        "--capability-inventory",
                        str(inventory),
                    ]
                )

            source = json.loads(output.getvalue())["source"]
            self.assertEqual(code, 0)
            self.assertEqual(source["adapter_version"], "2")
            self.assertEqual(source["capability_inventory"][0]["units"], ["tokens"])

    def test_cli_collect_persists_and_advances_bounded_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(3):
                core.atomic_json(
                    root
                    / ".orchestrator"
                    / "workstreams"
                    / f"stream-{index}"
                    / "workstream.json",
                    {
                        "schema_version": 1,
                        "kind": "ORCHESTRATOR_WORKSTREAM",
                        "workstream_id": f"stream-{index}",
                        "status": "active",
                        "created_at": "2026-09-08T10:00:00Z",
                    },
                )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main(["--project-root", temporary, "metrics", "init"]), 0
                )
                self.assertEqual(
                    cli.main(
                        [
                            "--project-root",
                            temporary,
                            "metrics",
                            "sources",
                            "register",
                            "--name",
                            "runtime",
                            "--type",
                            "orchestrator-engine",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    cli.main(
                        [
                            "--project-root",
                            temporary,
                            "metrics",
                            "collect",
                            "--source",
                            "runtime",
                            "--maximum",
                            "2",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    cli.main(
                        [
                            "--project-root",
                            temporary,
                            "metrics",
                            "collect",
                            "--source",
                            "runtime",
                            "--maximum",
                            "2",
                        ]
                    ),
                    0,
                )

            store = MetricsStore(root)
            self.assertEqual(len(store.observations()), 3)

    def test_cli_advisor_outage_falls_back_without_waiting_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        temporary,
                        "metrics",
                        "advise",
                        "--scope",
                        "operation_only",
                    ]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["next_action"], "use_ordinary_project_policy")
            self.assertNotIn("waiting_external", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
