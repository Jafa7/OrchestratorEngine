from __future__ import annotations

import unittest

from orchestrator_engine.metrics import contracts
from orchestrator_engine.metrics.calculations import calculate_metrics


class MetricCatalogExtensionTests(unittest.TestCase):
    SOURCE = "11111111-1111-4111-8111-111111111111"

    def item(
        self,
        record_type: str,
        data: dict[str, object],
        observation_id: str,
        observed_at: str,
    ) -> dict[str, object]:
        return contracts.make_observation(
            source_id=self.SOURCE,
            record_type=record_type,
            data=data,
            observation_id=observation_id,
            observed_at=observed_at,
        )

    def metrics(
        self, values: list[dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        return {item["metric_id"]: item for item in calculate_metrics(values)}

    def test_outcome_details_include_state_distribution_and_open_age(self) -> None:
        values = [
            self.item(
                "work_item",
                {
                    "work_item_id": "open",
                    "status": "blocked",
                    "started_at": "2026-09-01T10:00:00Z",
                },
                "open",
                "2026-09-03T10:00:00Z",
            ),
            self.item(
                "work_item",
                {
                    "work_item_id": "done",
                    "status": "accepted",
                    "started_at": "2026-09-01T10:00:00Z",
                },
                "done",
                "2026-09-03T10:00:00Z",
            ),
        ]

        details = self.metrics(values)["MET-001"]["details"]

        self.assertEqual(
            details["work_item_state_distribution"], {"accepted": 1, "blocked": 1}
        )
        self.assertEqual(details["open_item_count"], 1)
        self.assertEqual(details["open_age_seconds"]["p50"], 2 * 86400)

    def test_report_evaluation_time_controls_open_age(self) -> None:
        values = [
            self.item(
                "work_item",
                {
                    "work_item_id": "open",
                    "status": "blocked",
                    "started_at": "2026-09-01T10:00:00Z",
                },
                "open",
                "2026-09-02T10:00:00Z",
            )
        ]

        metrics = {
            item["metric_id"]: item
            for item in calculate_metrics(
                values, evaluation_time="2026-09-05T10:00:00Z"
            )
        }

        self.assertEqual(
            metrics["MET-001"]["details"]["open_age_seconds"]["p50"],
            4 * 86400,
        )

    def test_flow_details_include_deterministic_attempt_percentiles(self) -> None:
        values = [
            self.item(
                "execution_attempt",
                {
                    "execution_id": f"run-{index}",
                    "started_at": "2026-09-01T10:00:00Z",
                    "finished_at": f"2026-09-01T1{index}:00:00Z",
                },
                f"run-{index}",
                "2026-09-02T10:00:00Z",
            )
            for index in range(1, 6)
        ]

        percentiles = self.metrics(values)["MET-002"]["details"][
            "attempt_duration_seconds"
        ]

        self.assertEqual(percentiles["p50"], 3 * 3600)
        self.assertEqual(percentiles["p80"], 4 * 3600)
        self.assertEqual(percentiles["p95"], 5 * 3600)

    def test_quota_delta_is_never_computed_across_a_reset(self) -> None:
        values = [
            self.item(
                "quota_sample",
                {
                    "sample_id": "one",
                    "account_alias": "account",
                    "bucket_id": "weekly",
                    "window_id": "window-1",
                    "remaining_percentage_points": 80,
                    "reset_at": "2026-09-08T10:00:00Z",
                },
                "one",
                "2026-09-01T10:00:00Z",
            ),
            self.item(
                "quota_sample",
                {
                    "sample_id": "two",
                    "account_alias": "account",
                    "bucket_id": "weekly",
                    "window_id": "window-1",
                    "remaining_percentage_points": 65,
                    "reset_at": "2026-09-08T10:00:00Z",
                },
                "two",
                "2026-09-02T10:00:00Z",
            ),
            self.item(
                "quota_sample",
                {
                    "sample_id": "three",
                    "account_alias": "account",
                    "bucket_id": "weekly",
                    "window_id": "window-1",
                    "remaining_percentage_points": 100,
                    "reset_at": "2026-09-15T10:00:00Z",
                    "reset_occurred": True,
                },
                "three",
                "2026-09-08T10:00:00Z",
            ),
        ]

        details = self.metrics(values)["MET-008"]["details"]
        changes = details["observed_interval_changes"]

        self.assertEqual(changes[0]["remaining_delta_percentage_points"], -15)
        self.assertFalse(changes[0]["reset_discontinuity"])
        self.assertIsNone(changes[1]["remaining_delta_percentage_points"])
        self.assertTrue(changes[1]["reset_discontinuity"])
        self.assertEqual(changes[1]["attribution"], "shared_account_observation")


if __name__ == "__main__":
    unittest.main()
