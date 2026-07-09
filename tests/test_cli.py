from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_engine import cli, core, watcher, workers


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

    def test_worker_diagnose_reports_warnings_with_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                """
[workers.copilot-risky]
enabled = true
command = ["copilot", "--prompt"]
prompt_via = "arg"
""",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    ["--project-root", str(root), "worker", "diagnose"]
                )
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report["worst_severity"], "warning")
        self.assertEqual(
            report["workers"]["copilot-risky"]["diagnostics"][0]["code"],
            "copilot_may_request_approval",
        )

    def test_worker_diagnose_can_filter_to_info_only_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                """
[workers.quick]
enabled = true
command = ["python3", "-m", "pytest"]
prompt_via = "stdin"
""",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "worker",
                        "diagnose",
                        "--worker",
                        "quick",
                    ]
                )
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["worst_severity"], "info")
        self.assertEqual(
            report["workers"]["quick"]["diagnostics"][0]["code"],
            "worker_timeout_absent",
        )

    def test_worker_diagnose_rejects_unknown_worker_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                """
[workers.echo]
enabled = true
command = ["true"]
""",
                encoding="utf-8",
            )
            error_out = io.StringIO()
            with contextlib.redirect_stderr(error_out):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "worker",
                        "diagnose",
                        "--worker",
                        "missing",
                    ]
                )
        self.assertEqual(code, 1)
        self.assertIn("unknown worker: missing", error_out.getvalue())

    def test_worker_tasks_reports_runtime_diagnostics_with_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-BROKEN")
            task_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_json(task_dir / "evidence.json", {"ok": True})
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-BROKEN",
                    "worker": "echo",
                    "status": "completed",
                },
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "worker",
                        "tasks",
                        "--task-id",
                        "T-BROKEN",
                    ]
                )
        report = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(report["kind"], "WORKER_TASK_DIAGNOSTICS")
        self.assertEqual(
            report["tasks"]["T-BROKEN"]["diagnostics"][0]["code"],
            "task_missing_result",
        )

    def test_worker_tasks_can_filter_warning_diagnostics_to_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
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
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "worker",
                        "tasks",
                        "--severity",
                        "error",
                    ]
                )
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["diagnostic_count"], 0)
        self.assertEqual(report["tasks"]["T-FAIL"]["diagnostics"], [])

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

    def test_doctor_command_prints_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--project-root", str(root), "doctor"])

        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["kind"], "ORCHESTRATOR_DOCTOR_REPORT")
        self.assertEqual(report["status"], "warn")

    def test_doctor_command_strict_returns_nonzero_for_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    ["--project-root", str(root), "doctor", "--strict"]
                )

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "warn")

    def test_doctor_command_returns_nonzero_for_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core.atomic_json(
                core.events_root(root) / "future.json",
                {
                    "schema_version": 999,
                    "kind": "WORKER_TERMINAL",
                    "event_id": "future",
                },
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--project-root", str(root), "doctor"])

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")

    def test_adopt_command_scaffolds_and_reports_created_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    ["--project-root", str(root), "adopt", "--host", "claude"]
                )
            config_exists = (root / ".orchestrator" / "workers.toml").is_file()

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["kind"], "ORCHESTRATOR_ADOPTION")
        self.assertTrue(config_exists)
        self.assertIn("watcher stream", " ".join(result["next_steps"]))

    def test_adopt_command_dry_run_reports_plan_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--project-root", str(root), "adopt", "--dry-run"])
            state_exists = (root / ".orchestrator").exists()

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["dry_run"])
        self.assertFalse(state_exists)

    def test_adopt_command_reports_error_for_non_directory_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            missing = root / "missing"
            error_out = io.StringIO()
            with contextlib.redirect_stderr(error_out):
                code = cli.main(["--project-root", str(missing), "adopt"])

        self.assertEqual(code, 1)
        self.assertIn("project root is not a directory", error_out.getvalue())

    def test_doctor_and_adopt_require_single_project_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            errors = []
            for command in ("doctor", "adopt"):
                error_out = io.StringIO()
                with contextlib.redirect_stderr(error_out):
                    code = cli.main(
                        [
                            "--project-root",
                            first,
                            "--project-root",
                            second,
                            command,
                        ]
                    )
                errors.append((code, error_out.getvalue()))

        self.assertEqual([code for code, _error in errors], [1, 1])
        self.assertIn("doctor requires exactly one project root", errors[0][1])
        self.assertIn("adopt requires exactly one project root", errors[1][1])

    def test_adopt_and_doctor_respect_custom_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            adopt_out = io.StringIO()
            with contextlib.redirect_stdout(adopt_out):
                adopt_code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "--state-dir",
                        "local-orchestrator",
                        "adopt",
                    ]
                )
            doctor_out = io.StringIO()
            with contextlib.redirect_stdout(doctor_out):
                doctor_code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "--state-dir",
                        "local-orchestrator",
                        "doctor",
                    ]
                )

        self.assertEqual((adopt_code, doctor_code), (0, 0))
        adopt = json.loads(adopt_out.getvalue())
        doctor = json.loads(doctor_out.getvalue())
        self.assertEqual(adopt["state_dir"], "local-orchestrator")
        self.assertEqual(doctor["state_dir"], "local-orchestrator")

    def test_watcher_acknowledge_marks_event_seen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text('{"status":"ok"}', encoding="utf-8")
            evidence.write_text('{"review_ready":true}', encoding="utf-8")
            core.write_terminal_event(
                root,
                task_id="TASK-ACK",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-cli-ack",
            )
            state = watcher.default_state_path(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "watcher",
                        "--state-file",
                        str(state),
                        "acknowledge",
                        "--event-id",
                        "event-cli-ack",
                        "--reason",
                        "read manually",
                    ]
                )
            watcher_state = watcher.load_state(state)

        self.assertEqual(code, 0)
        ack = json.loads(output.getvalue())
        self.assertEqual(ack["status"], watcher.ACKNOWLEDGED_STATUS)
        self.assertEqual(ack["previous_status"], "pending")
        self.assertIn("event-cli-ack", watcher_state["seen_event_ids"])

    def test_watcher_deferred_list_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_state_path(root)
            core.atomic_json(
                state,
                {
                    "schema_version": 1,
                    "seen_event_ids": [],
                    "deferred_events": {
                        "event-cli-retry": {
                            "status": watcher.DEFER_STATUS_MANUAL_REQUIRED,
                            "attempts": 1,
                            "reason": "turn failed: usage limit",
                            "reason_code": "quota_or_usage_limit",
                            "task_id": "TASK-RETRY",
                        }
                    },
                    "acknowledged_events": {},
                },
            )
            list_out = io.StringIO()
            with contextlib.redirect_stdout(list_out):
                list_code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "watcher",
                        "--state-file",
                        str(state),
                        "deferred",
                        "list",
                    ]
                )
            retry_out = io.StringIO()
            with contextlib.redirect_stdout(retry_out):
                retry_code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "watcher",
                        "--state-file",
                        str(state),
                        "deferred",
                        "retry",
                        "--event-id",
                        "event-cli-retry",
                        "--reason",
                        "quota reset",
                    ]
                )
            watcher_state = watcher.load_state(state)

        self.assertEqual((list_code, retry_code), (0, 0))
        listing = json.loads(list_out.getvalue())
        self.assertEqual(listing["deferred_event_count"], 1)
        retry = json.loads(retry_out.getvalue())
        self.assertEqual(retry["status"], "retry_scheduled")
        self.assertEqual(
            watcher_state["deferred_events"]["event-cli-retry"]["status"],
            watcher.DEFER_STATUS_RETRYABLE,
        )

    def test_watcher_stream_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_stream_state_path(root, host="claude")
            core.atomic_json(
                state,
                {
                    "schema_version": 1,
                    "seen_event_ids": [],
                    "deferred_events": {},
                    "acknowledged_events": {},
                    "updated_at": core.utc_now(),
                },
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "watcher",
                        "--state-file",
                        str(state),
                        "stream",
                        "status",
                    ]
                )

        self.assertEqual(code, 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["kind"], "LOCAL_AI_ORCHESTRATOR_STREAM_STATUS")
        self.assertEqual(status["status"], "fresh")

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

    def test_callback_service_passes_host_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            calls: list[dict] = []

            def fake_start_service(*_args, **kwargs):
                calls.append(kwargs)
                return {"status": "running"}

            output = io.StringIO()
            with (
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
                        "--host",
                        "codex",
                        "--action",
                        "callback",
                        "service",
                        "start",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["host"], "codex")

    def test_deferred_commands_resolve_host_scoped_state(self) -> None:
        from orchestrator_engine import watcher

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            host_state = watcher.default_callback_state_path(root, host="codex")
            core.atomic_json(
                host_state,
                {
                    "schema_version": 1,
                    "seen_event_ids": [],
                    "deferred_events": {
                        "event-host": {
                            "attempts": 2,
                            "reason": "usage limit",
                            "status": "deferred_manual_required",
                            "last_attempt_at": 0,
                        }
                    },
                    "acknowledged_events": {},
                },
            )
            scoped_out = io.StringIO()
            with contextlib.redirect_stdout(scoped_out):
                scoped_code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "watcher",
                        "--host",
                        "codex",
                        "deferred",
                        "list",
                    ]
                )
            legacy_out = io.StringIO()
            with contextlib.redirect_stdout(legacy_out):
                legacy_code = cli.main(
                    ["--project-root", str(root), "watcher", "deferred", "list"]
                )
            retry_out = io.StringIO()
            with contextlib.redirect_stdout(retry_out):
                retry_code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "watcher",
                        "--host",
                        "codex",
                        "deferred",
                        "retry",
                        "--event-id",
                        "event-host",
                    ]
                )
        self.assertEqual((scoped_code, legacy_code, retry_code), (0, 0, 0))
        scoped = json.loads(scoped_out.getvalue())
        self.assertEqual(scoped["deferred_event_count"], 1)
        self.assertEqual(scoped["deferred_events"][0]["event_id"], "event-host")
        legacy = json.loads(legacy_out.getvalue())
        self.assertEqual(legacy["deferred_event_count"], 0)
        retried = json.loads(retry_out.getvalue())
        self.assertEqual(retried["status"], "retry_scheduled")
        self.assertEqual(retried["state_path"], str(host_state))

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
