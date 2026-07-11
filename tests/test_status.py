from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import (
    binding,
    cli,
    core,
    status,
    task_resolution,
    verification,
    workers,
)


def create_layout(root: Path) -> None:
    core.events_root(root).mkdir(parents=True, exist_ok=True)
    signals = core.inbox_root(root) / "signals"
    signals.mkdir(parents=True, exist_ok=True)


def write_worker_config(root: Path) -> None:
    config = workers.workers_config_path(root)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[workers.echo]
enabled = true
command = ["python3", "-c", "print('ok')"]
prompt_via = "stdin"
timeout_seconds = 10
""",
        encoding="utf-8",
    )


def write_failed_task(root: Path) -> None:
    task_dir = workers.task_dir_for(root, "T-FAIL")
    task_dir.mkdir(parents=True, exist_ok=True)
    core.atomic_json(task_dir / "result.json", {"terminal_status": "failed"})
    core.atomic_json(task_dir / "evidence.json", {"ok": True})
    core.atomic_json(
        task_dir / "task.json",
        {
            "schema_version": 1,
            "kind": workers.TASK_KIND,
            "task_id": "T-FAIL",
            "worker": "echo",
            "status": "failed",
        },
    )


def write_failed_check(root: Path) -> None:
    check_dir = verification.checks_root(root) / "CHECK-FAIL"
    check_dir.mkdir(parents=True, exist_ok=True)
    core.atomic_json(
        check_dir / "verification-result.json",
        {
            "schema_version": 1,
            "kind": verification.VERIFICATION_RESULT_KIND,
            "check_id": "CHECK-FAIL",
            "status": "failed",
            "exit_code": 1,
            "commands": [
                {
                    "label": "unit",
                    "required": True,
                    "status": "failed",
                    "exit_code": 1,
                    "command": "python -m unittest",
                    "log_path": ".orchestrator/checks/CHECK-FAIL/unit.log",
                }
            ],
            "summary_path": ".orchestrator/checks/CHECK-FAIL/summary.txt",
            "log_path": ".orchestrator/checks/CHECK-FAIL/full.log",
        },
    )
    (check_dir / "summary.txt").write_text("failed\n", encoding="utf-8")
    (check_dir / "full.log").write_text("failed\n", encoding="utf-8")
    (check_dir / "unit.log").write_text("failed\n", encoding="utf-8")


class StatusTests(unittest.TestCase):
    def test_status_reports_codex_history_only_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            report = status.run_status(root)

        channel = report["components"]["wake_channel"]
        self.assertEqual(channel["capabilities"]["live_refresh_support"], "unsupported")
        self.assertIn("cannot refresh", channel["detail"])
        self.assertIn("manual", channel["hint"])
        self.assertIn("Do not start", channel["hint"])

    def test_status_aggregates_compact_problem_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            write_failed_task(root)
            write_failed_check(root)
            report = status.run_status(root)

        self.assertEqual(report["kind"], status.STATUS_KIND)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["worst_severity"], "warning")
        self.assertIn("worker_tasks", report["components"])
        self.assertIn("checks", report["components"])
        self.assertIn(
            "T-FAIL",
            report["components"]["worker_tasks"]["problem_tasks"],
        )
        self.assertIn(
            "CHECK-FAIL",
            report["components"]["checks"]["problem_checks"],
        )
        self.assertNotIn(
            "supported",
            report["components"]["doctor"]["checks"][0],
        )
        self.assertGreaterEqual(report["issue_count"], 3)

    def test_status_cli_returns_warning_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--project-root", str(root), "status"])

        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report["kind"], status.STATUS_KIND)
        self.assertEqual(report["components"]["wake_channel"]["status"], "warn")

    def test_status_counts_resolved_failed_task_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            write_failed_task(root)
            task_resolution.write_resolution(
                root,
                task_id="T-FAIL",
                status="acknowledged",
                reason="Reviewed manually; superseded outside this fixture.",
            )
            report = status.run_status(root)

        tasks = report["components"]["worker_tasks"]
        self.assertEqual(tasks["resolved_task_count"], 1)
        self.assertEqual(tasks["problem_task_count"], 0)
        self.assertEqual(tasks["resolution_counts"]["acknowledged"], 1)
        self.assertFalse(
            any(
                issue.get("source") == "worker_tasks"
                for issue in report["issues"]
                if isinstance(issue, dict)
            )
        )

    def test_status_surfaces_large_logs_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            task_dir = workers.task_dir_for(root, "T-LOUD")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "result.json", {"terminal_status": "completed"})
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            (task_dir / "worker-stdout.log").write_text("x" * 64, encoding="utf-8")
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-LOUD",
                    "worker": "echo",
                    "status": "completed",
                },
            )
            report = status.run_status(root, large_log_bytes=16)
            draft = status.report_draft(
                root,
                project_name="Fixture",
                large_log_bytes=16,
            )

        tasks = report["components"]["worker_tasks"]
        self.assertEqual(tasks["problem_task_count"], 0)
        self.assertEqual(tasks["large_log_task_count"], 1)
        self.assertEqual(
            tasks["large_log_tasks"]["T-LOUD"]["large_logs"]["stdout"],
            64,
        )
        self.assertIn("## Large Worker Logs", draft)

    def test_status_surfaces_large_verification_logs_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            check_dir = verification.checks_root(root) / "CHECK-LOUD"
            check_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(
                check_dir / "verification-result.json",
                {
                    "schema_version": 1,
                    "kind": verification.VERIFICATION_RESULT_KIND,
                    "check_id": "CHECK-LOUD",
                    "status": "passed",
                    "exit_code": 0,
                    "commands": [],
                    "summary_path": ".orchestrator/checks/CHECK-LOUD/summary.txt",
                    "log_path": ".orchestrator/checks/CHECK-LOUD/full.log",
                },
            )
            (check_dir / "summary.txt").write_text("passed\n", encoding="utf-8")
            (check_dir / "full.log").write_text("x" * 64, encoding="utf-8")
            report = status.run_status(root, large_log_bytes=16)
            draft = status.report_draft(
                root,
                project_name="Fixture",
                large_log_bytes=16,
            )

        checks = report["components"]["checks"]
        self.assertEqual(checks["problem_check_count"], 0)
        self.assertEqual(checks["large_log_check_count"], 1)
        self.assertEqual(
            checks["large_log_checks"]["CHECK-LOUD"]["large_logs"]["full_log"],
            64,
        )
        self.assertIn("## Large Verification Logs", draft)

    def test_report_draft_returns_markdown_issue_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            draft = status.report_draft(root, project_name="Fixture")

        self.assertIn("# [runtime-report][Fixture]", draft)
        self.assertIn("## Component Status", draft)
        self.assertIn("## Issues", draft)
        self.assertIn("## Runtime Changes Made", draft)
        self.assertIn("`project:fixture`", draft)
        self.assertIn("`source:codex`", draft)
        self.assertIn("None by this report draft command", draft)

    def test_report_draft_lists_resolved_historical_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            write_failed_task(root)
            task_resolution.write_resolution(
                root,
                task_id="T-FAIL",
                status="acknowledged",
                reason="Reviewed manually.",
            )
            draft = status.report_draft(root, project_name="Fixture")

        self.assertIn("## Resolved Historical Tasks", draft)
        self.assertIn("task_id=`T-FAIL`", draft)
        self.assertIn("resolution=`acknowledged`", draft)

    def test_recommended_report_labels_are_stable_slugs(self) -> None:
        labels = status.recommended_report_labels(
            project_name="DocumentationEngine",
            report_type="runtime-report",
            host="VS Code",
        )

        self.assertEqual(
            labels,
            [
                "triage",
                "runtime-report",
                "project:documentationengine",
                "source:vs-code",
            ],
        )

    def test_report_draft_cli_can_write_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output_path = root / "report.md"
            create_layout(root)
            write_worker_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            code = cli.main(
                [
                    "--project-root",
                    str(root),
                    "report",
                    "draft",
                    "--project-name",
                    "Fixture",
                    "--output",
                    str(output_path),
                ]
            )
            draft = output_path.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertIn("[runtime-report][Fixture]", draft)


if __name__ == "__main__":
    unittest.main()
