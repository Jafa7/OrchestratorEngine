from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestrator_engine import core, task_diagnostics, task_resolution, workers


def write_task(root: Path, task_id: str, descriptor: dict) -> Path:
    task_dir = workers.task_dir_for(root, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    core.atomic_json(task_dir / "task.json", descriptor)
    return task_dir


def alive_only(*alive_pids: int):
    alive = set(alive_pids)

    def check(pid: int) -> bool:
        return pid in alive

    return check


class TaskDiagnosticTests(unittest.TestCase):
    def test_running_task_does_not_report_terminal_usage_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(
                root,
                "T-RUNNING-USAGE",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-RUNNING-USAGE",
                    "worker": "synthetic",
                    "status": "running",
                    "runtime_policy": {"soft_token_budget": 100},
                },
            )

            report = task_diagnostics.diagnose_tasks(
                root, process_checker=alive_only()
            )

        self.assertNotIn(
            "task_usage_measurement_incomplete",
            {
                item["code"]
                for item in report["tasks"]["T-RUNNING-USAGE"]["diagnostics"]
            },
        )

    def test_completed_process_without_required_verification_is_not_accepted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = write_task(
                root,
                "T-NO-ACCEPTANCE",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-NO-ACCEPTANCE",
                    "worker": "synthetic",
                    "status": "completed",
                    "task_intent": {"verification": "full"},
                },
            )
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(task_dir / "evidence.json", {"task_id": "T-NO-ACCEPTANCE"})

            report = task_diagnostics.diagnose_tasks(root)

        task = report["tasks"]["T-NO-ACCEPTANCE"]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["acceptance"]["status"], "evidence_missing")
        self.assertIn(
            "task_acceptance_evidence_missing",
            {item["code"] for item in task["diagnostics"]},
        )

    def test_completed_process_reports_verification_below_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = write_task(
                root,
                "T-LOW-ACCEPTANCE",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-LOW-ACCEPTANCE",
                    "worker": "synthetic",
                    "status": "completed",
                    "task_intent": {"verification": "full"},
                },
            )
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(
                task_dir / "evidence.json", {"task_id": "T-LOW-ACCEPTANCE"}
            )
            core.atomic_json(
                task_dir / "worker-handoff.json",
                {
                    "schema_version": 1,
                    "kind": "WORKER_HANDOFF",
                    "summary": "Focused checks passed.",
                    "verification": {
                        "level": "focused",
                        "status": "passed",
                        "checks": [{"name": "owning tests", "status": "passed"}],
                    },
                },
            )

            report = task_diagnostics.diagnose_tasks(root)

        task = report["tasks"]["T-LOW-ACCEPTANCE"]
        self.assertEqual(task["acceptance"]["status"], "below_required_level")
        self.assertIn(
            "task_acceptance_verification_below_intent",
            {item["code"] for item in task["diagnostics"]},
        )

    def test_completed_process_keeps_separate_evidenced_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = write_task(
                root,
                "T-ACCEPTED",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-ACCEPTED",
                    "worker": "synthetic",
                    "status": "completed",
                    "task_intent": {"verification": "focused"},
                },
            )
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(task_dir / "evidence.json", {"task_id": "T-ACCEPTED"})
            core.atomic_json(
                task_dir / "worker-handoff.json",
                {
                    "schema_version": 1,
                    "kind": "WORKER_HANDOFF",
                    "summary": "Full verification passed.",
                    "verification": {
                        "level": "full",
                        "status": "passed",
                        "checks": [
                            {"name": "release candidate", "status": "passed"}
                        ],
                    },
                },
            )

            report = task_diagnostics.diagnose_tasks(root)

        task = report["tasks"]["T-ACCEPTED"]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["acceptance"]["status"], "evidenced")
        self.assertNotIn(
            "task_acceptance_evidence_missing",
            {item["code"] for item in task["diagnostics"]},
        )

    def test_completed_task_preserves_profile_artifact_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-PLAN")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(
                task_dir / "evidence.json",
                {
                    "worker": "claude-readonly",
                    "command": [
                        "claude",
                        "-p",
                        "--permission-mode",
                        "plan",
                    ],
                    "worker_config": {"prompt_via": "stdin"},
                },
            )
            write_task(
                root,
                "T-PLAN",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-PLAN",
                    "worker": "claude-readonly",
                    "status": "completed",
                },
            )
            report = task_diagnostics.diagnose_tasks(root)

        self.assertEqual(report["diagnostic_count"], 1)
        self.assertEqual(
            report["tasks"]["T-PLAN"]["diagnostics"][0]["code"],
            "claude_plan_output_may_be_external",
        )

    def test_completed_plan_warning_can_be_durably_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-PLAN")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(
                task_dir / "evidence.json",
                {
                    "worker": "claude-readonly",
                    "command": ["claude", "-p", "--permission-mode", "plan"],
                    "worker_config": {"prompt_via": "stdin"},
                },
            )
            write_task(
                root,
                "T-PLAN",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-PLAN",
                    "worker": "claude-readonly",
                    "status": "completed",
                },
            )
            task_resolution.write_resolution(
                root,
                task_id="T-PLAN",
                status="acknowledged",
                reason="Complete stdout deliverable inspected.",
                diagnostic_codes=["claude_plan_output_may_be_external"],
            )
            report = task_diagnostics.diagnose_tasks(root)

        task = report["tasks"]["T-PLAN"]
        self.assertEqual(report["worst_severity"], "info")
        self.assertEqual(task["diagnostics"][0]["severity"], "info")
        self.assertEqual(
            task["diagnostics"][0]["code"],
            "claude_plan_output_may_be_external",
        )
        self.assertEqual(task["resolution"]["status"], "acknowledged")

    def test_completed_acknowledgement_does_not_hide_error_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-BROKEN")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            write_task(
                root,
                "T-BROKEN",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-BROKEN",
                    "worker": "echo",
                    "status": "completed",
                },
            )
            task_resolution.write_resolution(
                root,
                task_id="T-BROKEN",
                status="acknowledged",
                reason="Operator attempted to acknowledge the missing result.",
                diagnostic_codes=["task_missing_result"],
            )
            report = task_diagnostics.diagnose_tasks(root)

        diagnostic = report["tasks"]["T-BROKEN"]["diagnostics"][0]
        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(diagnostic["code"], "task_missing_result")
        self.assertEqual(diagnostic["severity"], "error")

    def test_completed_task_with_artifacts_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-OK")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            write_task(
                root,
                "T-OK",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-OK",
                    "worker": "echo",
                    "status": "completed",
                    "created_at": "2026-07-09T00:00:00.000+00:00",
                    "finished_at": "2026-07-09T00:00:01.000+00:00",
                },
            )
            report = task_diagnostics.diagnose_tasks(root)
        self.assertEqual(report["kind"], task_diagnostics.TASK_DIAGNOSTICS_KIND)
        self.assertEqual(report["diagnostic_count"], 0)
        self.assertIsNone(report["worst_severity"])
        self.assertEqual(report["tasks"]["T-OK"]["status"], "completed")
        self.assertEqual(report["status_counts"]["completed"], 1)
        self.assertIn("generated_at", report)

    def test_running_task_reports_dead_processes_and_stale_heartbeat(self) -> None:
        now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
        stale = now - timedelta(seconds=300)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(
                root,
                "T-STUCK",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-STUCK",
                    "worker": "slow",
                    "status": "running",
                    "created_at": stale.isoformat(timespec="milliseconds"),
                    "last_alive_at": stale.isoformat(timespec="milliseconds"),
                    "supervisor_pid": 111,
                    "worker_pid": 222,
                },
            )
            report = task_diagnostics.diagnose_tasks(
                root,
                stale_after_seconds=90,
                process_checker=alive_only(),
                now=now,
            )
        task = report["tasks"]["T-STUCK"]
        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(task["heartbeat_age_seconds"], 300.0)
        self.assertEqual(
            [item["code"] for item in task["diagnostics"]],
            ["task_supervisor_dead", "task_worker_dead", "task_heartbeat_stale"],
        )

    def test_terminal_task_reports_missing_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-BROKEN")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            write_task(
                root,
                "T-BROKEN",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-BROKEN",
                    "worker": "echo",
                    "status": "completed",
                },
            )
            report = task_diagnostics.diagnose_tasks(root)
        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(
            report["tasks"]["T-BROKEN"]["diagnostics"][0]["code"],
            "task_missing_result",
        )

    def test_unsuccessful_terminal_status_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-FAIL")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "failed"})
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            write_task(
                root,
                "T-FAIL",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-FAIL",
                    "worker": "echo",
                    "status": "failed",
                },
            )
            report = task_diagnostics.diagnose_tasks(root)
        self.assertEqual(report["worst_severity"], "warning")
        self.assertEqual(
            report["tasks"]["T-FAIL"]["diagnostics"][0]["code"],
            "task_terminal_unsuccessful",
        )

    def test_resolved_unsuccessful_terminal_status_is_info_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-FAIL")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "failed"})
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            write_task(
                root,
                "T-FAIL",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-FAIL",
                    "worker": "echo",
                    "status": "failed",
                },
            )
            task_resolution.write_resolution(
                root,
                task_id="T-FAIL",
                status="acknowledged",
                reason="Reviewed manually.",
            )
            report = task_diagnostics.diagnose_tasks(root)

        task = report["tasks"]["T-FAIL"]
        self.assertEqual(report["worst_severity"], "info")
        self.assertEqual(report["resolution_counts"]["acknowledged"], 1)
        self.assertEqual(task["resolution"]["status"], "acknowledged")
        self.assertEqual(
            task["diagnostics"][0]["code"],
            "task_terminal_unsuccessful_resolved",
        )

    def test_resolved_failed_task_still_reports_missing_artifact_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-FAIL")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            write_task(
                root,
                "T-FAIL",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-FAIL",
                    "worker": "echo",
                    "status": "failed",
                },
            )
            task_resolution.write_resolution(
                root,
                task_id="T-FAIL",
                status="acknowledged",
                reason="Reviewed manually.",
            )
            report = task_diagnostics.diagnose_tasks(root)

        codes = [item["code"] for item in report["tasks"]["T-FAIL"]["diagnostics"]]
        self.assertEqual(report["worst_severity"], "error")
        self.assertIn("task_terminal_unsuccessful_resolved", codes)
        self.assertIn("task_missing_result", codes)

    def test_large_worker_logs_are_reported_without_reading_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-LOUD")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            (task_dir / "worker-stdout.log").write_text("x" * 32, encoding="utf-8")
            (task_dir / "worker-stderr.log").write_text("small", encoding="utf-8")
            write_task(
                root,
                "T-LOUD",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-LOUD",
                    "worker": "echo",
                    "status": "completed",
                },
            )
            report = task_diagnostics.diagnose_tasks(root, large_log_bytes=16)

        task = report["tasks"]["T-LOUD"]
        self.assertEqual(report["worst_severity"], "info")
        self.assertEqual(task["log_sizes"]["stdout"], 32)
        self.assertEqual(
            task["diagnostics"][0]["code"],
            "task_large_worker_log",
        )
        self.assertEqual(task["diagnostics"][0]["severity"], "info")
        self.assertIn(
            "result.json/evidence.json first",
            task["diagnostics"][0]["suggested_action"],
        )

    def test_task_id_filter_rejects_missing_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(task_diagnostics.TaskDiagnosticError):
                task_diagnostics.diagnose_tasks(root, task_id="missing")

    def test_task_directory_without_descriptor_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workers.task_dir_for(root, "T-ORPHAN").mkdir(parents=True)
            report = task_diagnostics.diagnose_tasks(root)
        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(
            report["tasks"]["T-ORPHAN"]["diagnostics"][0]["code"],
            "task_descriptor_unreadable",
        )

    def test_task_id_mismatch_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-DIR")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            write_task(
                root,
                "T-DIR",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-DESCRIPTOR",
                    "worker": "echo",
                    "status": "completed",
                },
            )
            report = task_diagnostics.diagnose_tasks(root)
        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(
            report["tasks"]["T-DIR"]["diagnostics"][0]["code"],
            "task_id_mismatch",
        )

    def test_worker_filter_selects_matching_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for task_id, worker in (("T-ONE", "copilot"), ("T-TWO", "claude")):
                task_dir = workers.task_dir_for(root, task_id)
                task_dir.mkdir(parents=True, exist_ok=True)
                core.atomic_json(
                    task_dir / "result.json",
                    {"terminal_status": "completed"},
                )
                core.atomic_json(task_dir / "evidence.json", {"ok": True})
                write_task(
                    root,
                    task_id,
                    {
                        "schema_version": 1,
                        "kind": workers.TASK_KIND,
                        "task_id": task_id,
                        "worker": worker,
                        "status": "completed",
                    },
                )
            report = task_diagnostics.diagnose_tasks(root, worker="copilot")
        self.assertEqual(list(report["tasks"]), ["T-ONE"])
        self.assertEqual(report["filters"]["worker"], "copilot")

    def test_severity_filter_keeps_tasks_but_filters_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-FAIL")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "failed"})
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            write_task(
                root,
                "T-FAIL",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-FAIL",
                    "worker": "echo",
                    "status": "failed",
                },
            )
            report = task_diagnostics.diagnose_tasks(
                root,
                minimum_severity="error",
            )
        self.assertEqual(report["task_count"], 1)
        self.assertEqual(report["diagnostic_count"], 0)
        self.assertEqual(report["tasks"]["T-FAIL"]["diagnostics"], [])


if __name__ == "__main__":
    unittest.main()
