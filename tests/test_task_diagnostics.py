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
