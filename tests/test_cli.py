from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_bind_status_and_clear_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bind_out = io.StringIO()
            with contextlib.redirect_stdout(bind_out):
                bind_code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "bind",
                        "--host",
                        "codex",
                        "--thread-id",
                        "thread-1",
                    ]
                )
            status_out = io.StringIO()
            with contextlib.redirect_stdout(status_out):
                status_code = cli.main(
                    ["--project-root", str(root), "bind", "--status"]
                )
            clear_out = io.StringIO()
            with contextlib.redirect_stdout(clear_out):
                clear_code = cli.main(
                    ["--project-root", str(root), "bind", "--clear"]
                )
        self.assertEqual((bind_code, status_code, clear_code), (0, 0, 0))
        self.assertEqual(json.loads(bind_out.getvalue())["host"], "codex")
        status = json.loads(status_out.getvalue())
        self.assertEqual(status["target_thread_id"], "thread-1")
        self.assertEqual(json.loads(clear_out.getvalue())["status"], "cleared")

    def test_bind_codex_without_thread_id_fails_when_detection_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            error_out = io.StringIO()
            with (
                patch("orchestrator_engine.cli.codex_app.detect_thread_id",
                      return_value=None),
                contextlib.redirect_stderr(error_out),
            ):
                code = cli.main(
                    ["--project-root", str(root), "bind", "--host", "codex"]
                )
        self.assertEqual(code, 1)
        self.assertIn("thread id", error_out.getvalue())

    def test_bind_codex_auto_detects_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            detected = {
                "thread_id": "thread-auto",
                "source": "/mnt/c/Users/user/.codex/sessions/rollout.jsonl",
            }
            with (
                patch(
                    "orchestrator_engine.cli.codex_app.detect_thread_id",
                    return_value=detected,
                ),
                patch(
                    "orchestrator_engine.cli.codex_app.default_windows_codex",
                    return_value="/mnt/c/apps/codex.exe",
                ),
                contextlib.redirect_stdout(output),
            ):
                code = cli.main(
                    ["--project-root", str(root), "bind", "--host", "codex"]
                )
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["target_thread_id"], "thread-auto")
        self.assertEqual(result["thread_id_source"], detected["source"])
        self.assertEqual(result["codex_command"], "/mnt/c/apps/codex.exe")

    def test_worker_list_reports_empty_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--project-root", str(root), "worker", "list"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["workers"], {})

    def test_worker_run_reports_unknown_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            prompt = root / "prompt.md"
            prompt.write_text("task", encoding="utf-8")
            error_out = io.StringIO()
            with contextlib.redirect_stderr(error_out):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "worker",
                        "run",
                        "--worker",
                        "ghost",
                        "--task-id",
                        "T-1",
                        "--prompt-file",
                        str(prompt),
                    ]
                )
        self.assertEqual(code, 1)
        self.assertIn("no workers configured", error_out.getvalue())

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

    def test_callback_service_does_not_inherit_env_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            calls: list[dict] = []

            def fake_start_service(*_args, **kwargs):
                calls.append(kwargs)
                return {"status": "running"}

            output = io.StringIO()
            with (
                patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-env"}),
                patch(
                    "orchestrator_engine.cli.watcher.start_service",
                    side_effect=fake_start_service,
                ),
                contextlib.redirect_stdout(output),
            ):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "watcher",
                        "--action",
                        "callback",
                        "service",
                        "start",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertIsNone(calls[0]["target_thread_id"])

    def test_legacy_current_thread_callback_uses_env_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            calls: list[dict] = []

            def fake_start_service(*_args, **kwargs):
                calls.append(kwargs)
                return {"status": "running"}

            output = io.StringIO()
            with (
                patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-env"}),
                patch(
                    "orchestrator_engine.cli.watcher.start_service",
                    side_effect=fake_start_service,
                ),
                contextlib.redirect_stdout(output),
            ):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "watcher",
                        "--action",
                        "current-thread-callback",
                        "service",
                        "start",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["target_thread_id"], "thread-env")


if __name__ == "__main__":
    unittest.main()
