from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import cli, operation_wait


class OperationWaitTests(unittest.TestCase):
    def test_all_mode_aggregates_four_successful_operation_kinds(self) -> None:
        with (
            mock.patch.object(
                operation_wait.workers,
                "worker_wait_snapshot",
                return_value={
                    "status": "completed",
                    "terminal": True,
                    "worker": "cheap",
                    "result_path": "/state/tasks/T/result.json",
                },
            ),
            mock.patch.object(
                operation_wait.local_checks,
                "check_status",
                return_value={
                    "checks": [{"check_id": "C", "status": "passed"}],
                    "invalid": [],
                },
            ),
            mock.patch.object(
                operation_wait.github_actions,
                "monitor_status",
                return_value={
                    "monitors": [
                        {
                            "monitor_id": "G",
                            "status": "completed",
                            "ci_conclusion": "success",
                        }
                    ]
                },
            ),
            mock.patch.object(
                operation_wait.github_pull_requests,
                "monitor_status",
                return_value={
                    "monitors": [{"monitor_id": "P", "status": "ready"}]
                },
            ),
        ):
            snapshot = operation_wait.operation_wait_snapshot(
                Path("/project"),
                targets=["worker:T", "check:C", "ci:G", "pr:P"],
            )

        self.assertEqual(snapshot["status"], "completed")
        self.assertTrue(snapshot["condition_met"])
        self.assertEqual(snapshot["terminal_count"], 4)
        self.assertEqual(snapshot["successful_count"], 4)
        self.assertEqual(snapshot["active_count"], 0)
        self.assertNotIn("stdout", json.dumps(snapshot))

    @mock.patch.object(operation_wait.github_actions, "monitor_status")
    def test_ci_failure_is_terminal_and_unsuccessful(
        self, monitor_status: object
    ) -> None:
        monitor_status.return_value = {
            "monitors": [
                {
                    "monitor_id": "G",
                    "status": "completed",
                    "ci_conclusion": "failure",
                }
            ]
        }

        snapshot = operation_wait.operation_wait_snapshot(
            Path("/project"), targets=["ci:G"]
        )

        self.assertEqual(snapshot["status"], "unsuccessful")
        self.assertEqual(snapshot["unsuccessful_count"], 1)
        self.assertFalse(snapshot["targets"][0]["successful"])

    @mock.patch.object(operation_wait.local_checks, "check_status")
    def test_broken_supervisor_has_priority_over_completion(
        self, check_status: object
    ) -> None:
        check_status.return_value = {
            "checks": [
                {
                    "check_id": "C",
                    "status": "crashed",
                    "failure_kind": "supervisor_not_alive",
                }
            ],
            "invalid": [],
        }

        snapshot = operation_wait.operation_wait_snapshot(
            Path("/project"), targets=["check:C"], mode="any"
        )

        self.assertEqual(snapshot["wait_status"], "action_required")
        self.assertEqual(snapshot["action_required_targets"], ["check:C"])

    def test_wait_returns_after_state_transition_without_reading_logs(self) -> None:
        waiting = {
            "condition_met": False,
            "wait_status": "waiting",
            "status": "waiting",
        }
        completed = {
            "condition_met": True,
            "wait_status": "condition_met",
            "status": "completed",
        }
        clock = iter((0.0, 0.0, 1.0))
        sleeper = mock.Mock()
        with mock.patch.object(
            operation_wait,
            "operation_wait_snapshot",
            side_effect=[waiting, completed],
        ):
            snapshot = operation_wait.wait_for_operations(
                Path("/project"),
                targets=["worker:T"],
                monotonic=lambda: next(clock),
                sleeper=sleeper,
            )

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["waited_seconds"], 1.0)
        sleeper.assert_called_once_with(2.0)

    def test_wait_timeout_preserves_active_snapshot(self) -> None:
        waiting = {
            "condition_met": False,
            "wait_status": "waiting",
            "status": "waiting",
        }
        clock = iter((0.0, 2.0))
        with mock.patch.object(
            operation_wait,
            "operation_wait_snapshot",
            return_value=waiting,
        ):
            snapshot = operation_wait.wait_for_operations(
                Path("/project"),
                targets=["check:C"],
                timeout_seconds=1.0,
                monotonic=lambda: next(clock),
            )

        self.assertEqual(snapshot["wait_status"], "timed_out")
        self.assertEqual(snapshot["status"], "waiting")

    def test_targets_are_bounded_unique_and_typed(self) -> None:
        with self.assertRaisesRegex(operation_wait.OperationWaitError, "unique"):
            operation_wait.validate_targets(["worker:T", "worker:T"], mode="all")
        with self.assertRaisesRegex(operation_wait.OperationWaitError, "at most"):
            operation_wait.validate_targets(
                [f"worker:T-{index}" for index in range(65)], mode="all"
            )
        with self.assertRaisesRegex(operation_wait.OperationWaitError, "KIND:ID"):
            operation_wait.validate_targets(["unknown:T"], mode="all")

    @mock.patch("orchestrator_engine.cli.operation_wait.wait_for_operations")
    def test_cli_prints_bounded_json_and_returns_unsuccessful_exit(
        self, wait_for_operations: object
    ) -> None:
        wait_for_operations.return_value = {
            "schema_version": 1,
            "kind": "ORCHESTRATOR_OPERATION_WAIT_STATUS",
            "mode": "all",
            "status": "unsuccessful",
            "wait_status": "condition_met",
            "condition_met": True,
            "targets": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        temporary,
                        "operation",
                        "wait",
                        "--target",
                        "ci:G",
                        "--json",
                    ]
                )

        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(output.getvalue())["kind"],
            "ORCHESTRATOR_OPERATION_WAIT_STATUS",
        )

    @mock.patch("orchestrator_engine.cli.operation_wait.operation_wait_snapshot")
    def test_cli_status_prints_snapshot_without_waiting(
        self, operation_wait_snapshot: object
    ) -> None:
        operation_wait_snapshot.return_value = {
            "schema_version": 1,
            "kind": "ORCHESTRATOR_OPERATION_WAIT_STATUS",
            "mode": "all",
            "status": "waiting",
            "wait_status": "waiting",
            "condition_met": False,
            "targets": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        temporary,
                        "operation",
                        "status",
                        "--target",
                        "worker:T",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "waiting")
        operation_wait_snapshot.assert_called_once()

    def test_status_exit_codes_distinguish_results_from_active_state(self) -> None:
        self.assertEqual(
            cli.operation_status_exit_code(
                {
                    "status": "waiting",
                    "wait_status": "waiting",
                    "condition_met": False,
                }
            ),
            0,
        )
        self.assertEqual(
            cli.operation_status_exit_code(
                {
                    "status": "unsuccessful",
                    "wait_status": "condition_met",
                    "condition_met": True,
                }
            ),
            2,
        )
        self.assertEqual(
            cli.operation_status_exit_code(
                {
                    "status": "action_required",
                    "wait_status": "action_required",
                    "condition_met": False,
                }
            ),
            3,
        )


if __name__ == "__main__":
    unittest.main()
