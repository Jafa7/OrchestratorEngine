from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator_engine.metrics import contracts
from orchestrator_engine.metrics.calculations import latest_logical_records
from orchestrator_engine.metrics.progress import (
    build_progress_report,
    calculate_progress,
    markdown_progress,
)
from orchestrator_engine.metrics.reporting import build_report
from orchestrator_engine.metrics.store import MetricsStore


class ProgressCalculationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = "11111111-1111-4111-8111-111111111111"
        self.other_source = "22222222-2222-4222-8222-222222222222"
        self.index = 0

    def observation(
        self,
        record_type: str,
        data: dict[str, object],
        *,
        source_id: str | None = None,
        day: int = 1,
    ) -> dict[str, object]:
        self.index += 1
        timestamp = f"2026-08-{day:02d}T10:00:00Z"
        return contracts.make_observation(
            source_id=source_id or self.source,
            record_type=record_type,
            data=data,
            observation_id=f"observation-{self.index}",
            observed_at=timestamp,
            effective_at=timestamp,
            known_at=timestamp,
        )

    def revision(
        self,
        revision_id: str,
        index: int,
        *,
        baseline: bool = False,
        current: bool = False,
    ) -> dict[str, object]:
        return self.observation(
            "scope_revision",
            {
                "scope_revision_id": revision_id,
                "revision_index": index,
                "baseline": baseline,
                "current": current,
            },
        )

    def item(
        self,
        revision_id: str,
        item_id: str,
        *,
        status: str = "planned",
        module: str = "core",
        work_class: str = "feature",
        weight: float | None = None,
        day: int = 1,
        **extra: object,
    ) -> dict[str, object]:
        data: dict[str, object] = {
            "scope_revision_id": revision_id,
            "work_item_id": item_id,
            "criteria_revision": str(extra.pop("criteria_revision", "criteria-1")),
            "status": status,
            "module_id": module,
            "work_class": work_class,
            **extra,
        }
        if weight is not None:
            data["weight"] = weight
        return self.observation("scope_item", data, day=day)

    def acceptance(
        self,
        item_id: str,
        *,
        accepted: bool = True,
        evidence: bool = True,
        day: int = 10,
        revision_id: str = "scope-1",
        criteria_revision: str = "criteria-1",
        completion_cycle_id: str | None = None,
        source_id: str | None = None,
        **extra: object,
    ) -> dict[str, object]:
        data: dict[str, object] = {
            "work_item_id": item_id,
            "scope_revision_id": revision_id,
            "criteria_revision": criteria_revision,
            "accepted": accepted,
            "accepted_at": f"2026-08-{day:02d}T10:00:00Z",
            **extra,
        }
        if completion_cycle_id is not None or accepted:
            data["completion_cycle_id"] = completion_cycle_id or item_id
        if evidence:
            data["evidence_ref"] = f"evidence:{item_id}"
        return self.observation(
            "acceptance", data, day=day, source_id=source_id
        )

    def calculate(
        self, values: list[dict[str, object]], **kwargs: object
    ) -> dict[str, object]:
        return calculate_progress(
            values,
            project_source_ids={self.source},
            minimum_samples=5,
            **kwargs,
        )

    def test_project_owner_source_and_scope_revision_are_required(self) -> None:
        values = [self.revision("scope-1", 1, current=True)]

        unavailable = calculate_progress(values, project_source_ids=set())
        missing_items = self.calculate(values)

        self.assertEqual(unavailable["reason"], "project_owner_source_unavailable")
        self.assertEqual(missing_items["reason"], "current_scope_items_unavailable")

    def test_baseline_current_progress_and_scope_growth_are_separate(self) -> None:
        values = [
            self.revision("scope-1", 1, baseline=True),
            self.item("scope-1", "one", weight=2),
            self.item("scope-1", "two", weight=1),
            self.revision("scope-2", 2, current=True),
            self.item("scope-2", "one", status="accepted", weight=2),
            self.item("scope-2", "two", weight=1),
            self.item(
                "scope-2",
                "three",
                weight=3,
                change_reason="external_change",
            ),
            self.acceptance("one", revision_id="scope-1"),
            self.acceptance(
                "one",
                revision_id="scope-2",
                carried_from_scope_revision_id="scope-1",
            ),
        ]

        result = self.calculate(values)

        self.assertEqual(result["status"], "available")
        self.assertAlmostEqual(result["baseline"]["progress_percent"], 200 / 3)
        self.assertAlmostEqual(result["current"]["progress_percent"], 100 / 3)
        self.assertEqual(result["scope_change"]["added_count"], 1)
        self.assertEqual(result["scope_change"]["growth_percent"], 100.0)
        self.assertEqual(result["scope_change"]["change_reasons"], ["external_change"])

    def test_reopened_or_unproven_acceptance_does_not_count(self) -> None:
        values = [
            self.revision("scope-1", 1, baseline=True, current=True),
            self.item("scope-1", "one", status="accepted"),
            self.item("scope-1", "two", status="accepted"),
            self.acceptance("one", evidence=False, day=5),
            self.acceptance("two", day=5),
            self.acceptance("two", accepted=False, day=6),
        ]

        result = self.calculate(values)

        self.assertEqual(result["current"]["accepted_count"], 0)
        self.assertEqual(result["current"]["accepted_without_evidence_count"], 2)
        self.assertEqual(result["current"]["progress_percent"], 0)

    def test_acceptance_does_not_cross_scope_or_criteria_revision(self) -> None:
        values = [
            self.revision("scope-1", 1, baseline=True),
            self.item("scope-1", "one", status="accepted"),
            self.acceptance("one", revision_id="scope-1"),
            self.revision("scope-2", 2, current=True),
            self.item(
                "scope-2",
                "one",
                status="reopened",
                criteria_revision="criteria-2",
            ),
        ]

        result = self.calculate(values)

        self.assertEqual(result["baseline"]["accepted_count"], 1)
        self.assertEqual(result["current"]["accepted_count"], 0)
        self.assertEqual(result["current"]["progress_percent"], 0)

    def test_conflicting_project_owner_acceptance_fails_closed(self) -> None:
        values = [
            self.revision("scope-1", 1, baseline=True, current=True),
            self.item("scope-1", "one", status="accepted"),
            self.acceptance("one", day=4),
            self.acceptance(
                "one",
                accepted=False,
                day=5,
                source_id=self.other_source,
            ),
        ]

        result = calculate_progress(
            values,
            project_source_ids={self.source, self.other_source},
        )

        self.assertEqual(result["current"]["accepted_count"], 0)

    def test_module_filter_and_module_rollup(self) -> None:
        values = [
            self.revision("scope-1", 1, baseline=True, current=True),
            self.item("scope-1", "core-one", module="core"),
            self.item("scope-1", "cli-one", module="cli"),
            self.acceptance("core-one"),
        ]

        whole = self.calculate(values)
        core = self.calculate(values, module_id="core")

        self.assertEqual(
            [item["module_id"] for item in whole["modules"]], ["cli", "core"]
        )
        self.assertEqual(core["current"]["item_count"], 1)
        self.assertEqual(core["current"]["progress_percent"], 100)

    def test_removed_item_reason_is_preserved_from_current_revision(self) -> None:
        values = [
            self.revision("scope-1", 1, baseline=True),
            self.item("scope-1", "removed"),
            self.revision("scope-2", 2, current=True),
            self.item(
                "scope-2",
                "removed",
                status="removed",
                change_reason="user_direction",
            ),
            self.item("scope-2", "remaining"),
        ]

        result = self.calculate(values)

        self.assertEqual(result["scope_change"]["removed_count"], 1)
        self.assertEqual(result["scope_change"]["change_reasons"], ["user_direction"])

    def test_forecast_is_unavailable_without_five_comparable_samples(self) -> None:
        values = [
            self.revision("scope-1", 1, baseline=True, current=True),
            self.item("scope-1", "done", started_at="2026-08-01T10:00:00Z"),
            self.item("scope-1", "remaining"),
            self.acceptance("done", day=2),
        ]

        result = self.calculate(values)

        self.assertEqual(result["forecast"]["status"], "unavailable")
        self.assertEqual(
            result["forecast"]["reason"], "insufficient_comparable_history"
        )
        self.assertIsNone(
            result["forecast"]["sequential_effort_scenario_days_p50"]
        )

    def test_forecast_uses_deterministic_empirical_p50_and_p80(self) -> None:
        values = [self.revision("scope-1", 1, baseline=True, current=True)]
        for index, duration_days in enumerate((1, 2, 3, 4, 5), start=1):
            item_id = f"done-{index}"
            values.extend(
                [
                    self.item(
                        "scope-1",
                        item_id,
                        status="accepted",
                        started_at="2026-08-01T10:00:00Z",
                    ),
                    self.acceptance(item_id, day=1 + duration_days),
                ]
            )
        values.extend(
            [
                self.item("scope-1", "remaining-one"),
                self.item("scope-1", "remaining-two"),
            ]
        )

        result = self.calculate(values)
        forecast = result["forecast"]

        self.assertEqual(forecast["status"], "available")
        self.assertEqual(forecast["sample_count"], 5)
        self.assertEqual(forecast["sequential_effort_scenario_days_p50"], 6)
        self.assertEqual(forecast["sequential_effort_scenario_days_p80"], 8)
        self.assertIsNone(forecast["calendar_finish_date"])

    def test_forecast_clamps_blocked_time_to_the_observed_cycle(self) -> None:
        values = [self.revision("scope-1", 1, baseline=True, current=True)]
        for index in range(5):
            item_id = f"done-{index}"
            values.extend(
                [
                    self.item(
                        "scope-1",
                        item_id,
                        status="accepted",
                        started_at="2026-08-01T10:00:00Z",
                        blocked_seconds=2 * 86400,
                    ),
                    self.acceptance(item_id, day=2),
                ]
            )
        values.append(self.item("scope-1", "remaining"))

        forecast = self.calculate(values)["forecast"]

        self.assertEqual(forecast["active_effort_days_p50"], 0)
        self.assertEqual(forecast["blocked_days_p50"], 1)
        self.assertEqual(forecast["sequential_effort_scenario_days_p50"], 1)

    def test_total_cycle_scenario_uses_paired_samples(self) -> None:
        values = [self.revision("scope-1", 1, baseline=True, current=True)]
        for index, blocked_days in enumerate((1, 3, 5, 7, 9)):
            item_id = f"done-{index}"
            values.extend(
                [
                    self.item(
                        "scope-1",
                        item_id,
                        status="accepted",
                        started_at="2026-08-01T10:00:00Z",
                        blocked_seconds=blocked_days * 86400,
                    ),
                    self.acceptance(item_id, day=11),
                ]
            )
        values.append(self.item("scope-1", "remaining"))

        forecast = self.calculate(values)["forecast"]

        self.assertEqual(forecast["sequential_effort_scenario_days_p80"], 10)
        self.assertEqual(
            forecast["aggregation_semantics"],
            "sum_of_work_class_cycle_scenarios",
        )

    def test_native_identity_is_source_scoped_and_canonical_identity_can_merge(
        self,
    ) -> None:
        independent = [
            self.observation(
                "execution_attempt",
                {"execution_id": "same"},
                source_id=self.source,
            ),
            self.observation(
                "execution_attempt",
                {"execution_id": "same"},
                source_id=self.other_source,
            ),
        ]
        canonical = [
            self.observation(
                "execution_attempt",
                {
                    "execution_id": "left",
                    "canonical_identity": {"namespace": "ci", "id": "run-1"},
                },
                source_id=self.source,
            ),
            self.observation(
                "execution_attempt",
                {
                    "execution_id": "right",
                    "canonical_identity": {"namespace": "ci", "id": "run-1"},
                },
                source_id=self.other_source,
            ),
        ]

        self.assertEqual(len(latest_logical_records(independent)), 2)
        self.assertEqual(len(latest_logical_records(canonical)), 2)
        self.assertEqual(
            len(
                latest_logical_records(
                    canonical,
                    canonical_source_ids={self.source, self.other_source},
                )
            ),
            1,
        )

    def test_canonical_identity_parts_cannot_collide_on_delimiters(self) -> None:
        values = [
            self.observation(
                "execution_attempt",
                {
                    "execution_id": f"run-{index}",
                    "canonical_identity": {"namespace": namespace, "id": value},
                },
                source_id=source,
            )
            for index, (source, namespace, value) in enumerate(
                (
                    (self.source, "a:b", "c"),
                    (self.other_source, "a", "b:c"),
                )
            )
        ]

        records = latest_logical_records(
            values,
            canonical_source_ids={self.source, self.other_source},
        )

        self.assertEqual(len(records), 2)

    def test_reused_completion_cycle_is_one_forecast_sample(self) -> None:
        values = []
        for index in range(1, 6):
            revision_id = f"scope-{index}"
            values.extend(
                [
                    self.revision(revision_id, index, baseline=index == 1),
                    self.item(
                        revision_id,
                        "same-work",
                        status="accepted",
                        started_at="2026-08-01T10:00:00Z",
                    ),
                    self.acceptance(
                        "same-work",
                        revision_id=revision_id,
                        day=2,
                        completion_cycle_id="one-real-cycle",
                    ),
                ]
            )
        values.extend(
            [
                self.revision("scope-6", 6, current=True),
                self.item("scope-6", "remaining"),
            ]
        )

        result = self.calculate(values)

        self.assertEqual(
            result["forecast"]["reason"], "insufficient_comparable_history"
        )
        self.assertEqual(
            result["forecast"]["details"]["available_samples_by_work_class"],
            {"feature": 1},
        )

    def test_scope_contract_rejects_invalid_weight_and_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive number"):
            self.item("scope-1", "bad", weight=0)
        with self.assertRaisesRegex(ValueError, "unsupported scope item status"):
            self.item("scope-1", "bad", status="finished")
        with self.assertRaisesRegex(ValueError, "canonical_identity must be an object"):
            self.observation(
                "execution_attempt",
                {"execution_id": "bad", "canonical_identity": "bad"},
            )
        with self.assertRaisesRegex(ValueError, "criteria_revision"):
            self.observation(
                "scope_item",
                {
                    "scope_revision_id": "scope-1",
                    "work_item_id": "bad",
                    "status": "planned",
                },
            )
        with self.assertRaisesRegex(ValueError, "scope_revision_id"):
            self.observation(
                "acceptance",
                {
                    "work_item_id": "bad",
                    "criteria_revision": "criteria-1",
                    "accepted": True,
                },
            )


class ProgressReportTests(unittest.TestCase):
    def test_report_is_generation_pinned_and_markdown_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricsStore(Path(temporary))
            store.initialize()
            source = store.register_source(
                name="project-plan",
                source_type="project_scope",
                authority="project_owner",
                capabilities=["scope", "acceptance"],
            )["source"]
            values = [
                contracts.make_observation(
                    source_id=source["source_id"],
                    record_type="scope_revision",
                    data={
                        "scope_revision_id": "scope-1",
                        "revision_index": 1,
                        "baseline": True,
                        "current": True,
                    },
                    observation_id="revision",
                    observed_at="2026-08-01T10:00:00Z",
                ),
                contracts.make_observation(
                    source_id=source["source_id"],
                    record_type="scope_item",
                    data={
                        "scope_revision_id": "scope-1",
                        "work_item_id": "one",
                        "criteria_revision": "criteria-1",
                        "status": "planned",
                        "module_id": "core",
                        "work_class": "feature",
                    },
                    observation_id="item",
                    observed_at="2026-08-01T10:00:00Z",
                ),
            ]
            store.ingest(values)

            report = build_progress_report(
                store, evaluation_time="2026-08-02T10:00:00Z"
            )
            markdown = markdown_progress(report)

            self.assertEqual(report["kind"], contracts.PROGRESS_KIND)
            self.assertEqual(report["status"], "available")
            self.assertIn(report["generation_digest"], markdown)
            self.assertIn("Accepted progress: 0.0%", markdown)

    def test_mutable_acceptance_snapshot_does_not_inherit_old_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricsStore(Path(temporary))
            store.initialize()
            source = store.register_source(
                name="project-plan",
                source_type="project_scope",
                authority="project_owner",
                observation_semantics="mutable_snapshots",
                capabilities=["scope", "acceptance"],
            )["source"]
            values = [
                contracts.make_observation(
                    source_id=source["source_id"],
                    record_type="scope_revision",
                    data={
                        "scope_revision_id": "scope-1",
                        "revision_index": 1,
                        "current": True,
                    },
                    observation_id="revision",
                    observed_at="2026-08-01T10:00:00Z",
                ),
                contracts.make_observation(
                    source_id=source["source_id"],
                    record_type="scope_item",
                    data={
                        "scope_revision_id": "scope-1",
                        "work_item_id": "one",
                        "criteria_revision": "criteria-1",
                        "status": "accepted",
                    },
                    observation_id="item",
                    observed_at="2026-08-01T10:00:00Z",
                ),
            ]
            for day, accepted, evidence in (
                (4, True, True),
                (5, False, False),
                (6, True, False),
            ):
                data = {
                    "work_item_id": "one",
                    "scope_revision_id": "scope-1",
                    "criteria_revision": "criteria-1",
                    "accepted": accepted,
                }
                if evidence:
                    data["evidence_ref"] = "evidence:old"
                values.append(
                    contracts.make_observation(
                        source_id=source["source_id"],
                        record_type="acceptance",
                        data=data,
                        observation_id=f"acceptance-{day}",
                        observed_at=f"2026-08-{day:02d}T10:00:00Z",
                    )
                )
            store.ingest(values)

            report = build_progress_report(
                store, evaluation_time="2026-08-07T10:00:00Z"
            )

            self.assertEqual(report["current"]["accepted_count"], 0)

    def test_report_merges_only_sources_with_authorized_canonical_mapping(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricsStore(Path(temporary))
            store.initialize()
            sources = [
                store.register_source(
                    name=f"mirror-{index}",
                    source_type="fixture",
                    identity_mapping="canonical_identity_authorized",
                )["source"]
                for index in range(2)
            ]
            values = [
                contracts.make_observation(
                    source_id=source["source_id"],
                    record_type="execution_attempt",
                    data={
                        "execution_id": f"native-{index}",
                        "canonical_identity": {
                            "namespace": "ci-provider",
                            "id": "run-1",
                        },
                        "started_at": "2026-08-01T10:00:00Z",
                        "finished_at": "2026-08-01T11:00:00Z",
                    },
                    observation_id=f"mirror-{index}",
                    observed_at="2026-08-01T11:00:00Z",
                )
                for index, source in enumerate(sources)
            ]
            store.ingest(values)

            report = build_report(
                store, evaluation_time="2026-08-02T10:00:00Z"
            )
            active_time = next(
                item for item in report["metrics"] if item["metric_id"] == "MET-002"
            )

            self.assertEqual(active_time["coverage"]["total"], 1)
            self.assertEqual(active_time["value"], 3600)


if __name__ == "__main__":
    unittest.main()
