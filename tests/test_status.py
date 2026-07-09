from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import binding, cli, core, status, verification, workers


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


if __name__ == "__main__":
    unittest.main()
