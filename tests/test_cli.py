from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import cli, core


class CliTests(unittest.TestCase):
    def test_emit_and_inbox_round_trip_through_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text('{"status":"ok"}', encoding="utf-8")
            evidence.write_text('{"review_ready":true}', encoding="utf-8")

            emit_out = io.StringIO()
            with contextlib.redirect_stdout(emit_out):
                emit_code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "emit",
                        "--task-id",
                        "TASK-001",
                        "--terminal-status",
                        "completed",
                        "--result",
                        str(result),
                        "--evidence",
                        str(evidence),
                        "--event-id",
                        "event-1",
                    ]
                )

            inbox_out = io.StringIO()
            with contextlib.redirect_stdout(inbox_out):
                inbox_code = cli.main(["--project-root", str(root), "inbox"])

        self.assertEqual(emit_code, 0)
        self.assertEqual(inbox_code, 0)
        emitted = json.loads(emit_out.getvalue())
        self.assertEqual(emitted["event"]["task_id"], "TASK-001")
        inbox = json.loads(inbox_out.getvalue())
        self.assertEqual(inbox[str(root)][0]["event_id"], "event-1")

    def test_main_reports_error_and_returns_nonzero_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            error_out = io.StringIO()
            with contextlib.redirect_stderr(error_out):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "emit",
                        "--task-id",
                        "TASK-001",
                        "--terminal-status",
                        "completed",
                        "--result",
                        str(root / "missing-result.json"),
                        "--evidence",
                        str(root / "missing-evidence.json"),
                    ]
                )

        self.assertEqual(code, 1)
        self.assertIn("ERROR:", error_out.getvalue())

    def test_cleanup_dry_run_reports_zero_removals_on_empty_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core.inbox_root(root).mkdir(parents=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--project-root", str(root), "cleanup", "--dry-run"])

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["removed_count"], 0)
        self.assertTrue(result["dry_run"])


if __name__ == "__main__":
    unittest.main()
