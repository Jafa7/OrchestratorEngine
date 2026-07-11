from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import ClassVar

from orchestrator_engine import binding, core, workers

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_RUNNER = REPO_ROOT / "examples" / "check_runner.py"

WORKERS_TOML = """
[workers.echo]
enabled = true
command = ["{python}", "-c", "import sys; print('worker ran'); sys.exit(0)"]
prompt_via = "stdin"
effort = "high"

[workers.failing]
enabled = true
command = ["{python}", "-c", "import sys; sys.exit(3)"]
prompt_via = "stdin"

[workers.disabled]
enabled = false
command = ["true"]

[workers.sleeper]
enabled = true
command = ["{python}", "-c", "import sys,time; sys.stdin.read(); time.sleep(0.7)"]
prompt_via = "stdin"

[workers.stuck]
enabled = true
command = ["{python}", "-c", "import time; time.sleep(30)"]
prompt_via = "stdin"
timeout_seconds = 1

[workers.long]
enabled = true
command = ["{python}", "-c", "import sys; sys.stdin.read(); print('done')"]
prompt_via = "stdin"
expect_long_running = true

[workers.copilot-risky]
enabled = true
command = ["copilot", "--prompt"]
prompt_via = "arg"

[workers.copilot-safe]
enabled = true
command = ["copilot", "--prompt", "--allow-all", "--no-ask-user"]
prompt_via = "arg"

[workers.codex-risky]
enabled = true
command = ["codex", "exec", "--json"]
prompt_via = "arg"

[workers.codex-safe]
enabled = true
command = ["codex", "exec", "--json",
           "-c", "approval_policy=\\"never\\"",
           "-c", "sandbox_mode=\\"danger-full-access\\""]
prompt_via = "arg"

[workers.claude-risky]
enabled = true
command = ["claude", "-p", "--model", "sonnet"]
prompt_via = "stdin"

[workers.claude-safe]
enabled = true
command = ["claude", "-p", "--model", "sonnet",
           "--permission-mode", "acceptEdits"]
prompt_via = "stdin"
""".replace("{python}", sys.executable)


class FakePopen:
    command: ClassVar[list[str]] = []
    kwargs: ClassVar[dict[str, object]] = {}

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.__class__.command = command
        self.__class__.kwargs = kwargs
        self.pid = 5150


def write_config(root: Path) -> None:
    path = workers.workers_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKERS_TOML, encoding="utf-8")


def write_prompt(root: Path) -> Path:
    prompt = root / "prompt.md"
    prompt.write_text("do the task", encoding="utf-8")
    return prompt


class WorkerRegistryTests(unittest.TestCase):
    def test_example_registry_maps_gpt_56_capability_tiers(self) -> None:
        catalog = tomllib.loads(
            (REPO_ROOT / "examples" / "workers.toml").read_text(encoding="utf-8")
        )["workers"]

        expected_models = {
            "codex-fast": "gpt-5.6-luna",
            "codex": "gpt-5.6-terra",
            "codex-deep": "gpt-5.6-sol",
        }
        for profile, model in expected_models.items():
            command = catalog[profile]["command"]
            self.assertFalse(catalog[profile]["enabled"])
            self.assertEqual(command[command.index("-m") + 1], model)
            self.assertIn('approval_policy="never"', command)

        for profile, config in catalog.items():
            validated = workers.validate_worker_config(profile, config)
            self.assertEqual(validated["warnings"], [], profile)

    def test_list_workers_reports_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            listing = workers.list_workers(root)
        self.assertIn("echo", listing["workers"])
        self.assertTrue(listing["workers"]["echo"]["enabled"])
        self.assertFalse(listing["workers"]["disabled"]["enabled"])
        self.assertEqual(listing["workers"]["echo"]["effort"], "high")
        self.assertFalse(listing["workers"]["echo"]["expect_long_running"])
        self.assertTrue(listing["workers"]["long"]["expect_long_running"])
        self.assertEqual(listing["workers"]["echo"]["warnings"], [])

    def test_list_workers_warns_for_copilot_without_noninteractive_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            listing = workers.list_workers(root)
        risky = listing["workers"]["copilot-risky"]
        safe = listing["workers"]["copilot-safe"]
        self.assertEqual(
            risky["warnings"][0]["code"],
            "copilot_may_request_approval",
        )
        self.assertIn("--no-ask-user", risky["warnings"][0]["message"])
        self.assertEqual(safe["warnings"], [])

    def test_list_workers_warns_for_codex_without_noninteractive_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            listing = workers.list_workers(root)
        risky = listing["workers"]["codex-risky"]
        safe = listing["workers"]["codex-safe"]
        self.assertEqual(
            [warning["code"] for warning in risky["warnings"]],
            ["codex_may_request_approval", "codex_missing_sandbox_strategy"],
        )
        self.assertEqual(safe["warnings"], [])

    def test_list_workers_warns_for_claude_without_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            listing = workers.list_workers(root)
        risky = listing["workers"]["claude-risky"]
        safe = listing["workers"]["claude-safe"]
        self.assertEqual(
            risky["warnings"][0]["code"],
            "claude_missing_permission_mode",
        )
        self.assertEqual(safe["warnings"], [])

    def test_diagnose_workers_reports_info_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            report = workers.diagnose_workers(root)
        self.assertEqual(report["kind"], "WORKER_DIAGNOSTICS")
        self.assertEqual(report["worst_severity"], "warning")
        self.assertGreater(report["severity_counts"]["info"], 0)
        self.assertGreater(report["severity_counts"]["warning"], 0)
        self.assertEqual(
            report["workers"]["echo"]["diagnostics"][0]["code"],
            "worker_timeout_absent",
        )
        self.assertEqual(report["workers"]["echo"]["metadata"]["effort"], "high")
        self.assertEqual(report["workers"]["long"]["diagnostics"], [])

    def test_diagnose_workers_filters_by_worker_and_severity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            report = workers.diagnose_workers(
                root,
                worker="codex-risky",
                minimum_severity="warning",
            )
        self.assertEqual(list(report["workers"]), ["codex-risky"])
        self.assertEqual(report["diagnostic_count"], 2)
        self.assertEqual(report["severity_counts"]["info"], 0)
        self.assertEqual(report["worst_severity"], "warning")

    def test_diagnose_workers_can_filter_enabled_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            report = workers.diagnose_workers(root, enabled_only=True)
        self.assertNotIn("disabled", report["workers"])
        self.assertIn("echo", report["workers"])

    def test_diagnose_workers_rejects_unknown_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            with self.assertRaises(workers.WorkerError):
                workers.diagnose_workers(root, worker="missing")

    def test_invalid_expect_long_running_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = workers.workers_config_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                """
[workers.bad]
command = ["true"]
expect_long_running = "yes"
""",
                encoding="utf-8",
            )
            with self.assertRaises(workers.WorkerError):
                workers.load_registry(root)

    def test_missing_config_yields_empty_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            listing = workers.list_workers(root)
        self.assertEqual(listing["workers"], {})

    def test_require_worker_rejects_disabled_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            with self.assertRaises(workers.WorkerError):
                workers.require_worker(root, "disabled")
            with self.assertRaises(workers.WorkerError):
                workers.require_worker(root, "missing")

    def test_invalid_task_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(workers.WorkerError):
                workers.task_dir_for(root, "../escape")


class WorkerRunTests(unittest.TestCase):
    def test_run_worker_spawns_detached_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            descriptor = workers.run_worker(
                root,
                worker="echo",
                task_id="T-1",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            stored = core.load_object(Path(descriptor["descriptor_path"]))
        self.assertEqual(descriptor["status"], "running")
        self.assertEqual(stored["supervisor_pid"], 5150)
        self.assertIn("supervise", FakePopen.command)
        self.assertTrue(FakePopen.kwargs["start_new_session"])

    def test_run_worker_refuses_duplicate_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            workers.run_worker(
                root,
                worker="echo",
                task_id="T-1",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            with self.assertRaises(workers.WorkerError):
                workers.run_worker(
                    root,
                    worker="echo",
                    task_id="T-1",
                    prompt_file=prompt,
                    popen_factory=FakePopen,
                )

    def test_run_worker_captures_current_wake_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            binding.write_binding(
                root,
                host="codex",
                target_thread_id="thread-origin",
                codex_command="/mnt/c/apps/codex.exe",
            )
            prompt = write_prompt(root)
            descriptor = workers.run_worker(
                root,
                worker="echo",
                task_id="T-WAKE",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            stored = core.load_object(Path(descriptor["descriptor_path"]))
        self.assertEqual(stored["wake_target"]["host"], "codex")
        self.assertEqual(stored["wake_target"]["target_thread_id"], "thread-origin")
        self.assertEqual(
            stored["wake_target"]["codex_command"],
            "/mnt/c/apps/codex.exe",
        )

    def test_run_worker_returns_profile_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            descriptor = workers.run_worker(
                root,
                worker="copilot-risky",
                task_id="T-WARN",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
        self.assertEqual(
            descriptor["warnings"][0]["code"],
            "copilot_may_request_approval",
        )


class WorkerSuperviseTests(unittest.TestCase):
    def test_supervise_success_emits_completed_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="echo",
                task_id="T-OK",
                prompt_file=prompt,
            )
            event = core.load_object(Path(summary["event_path"]))
            signals = core.inbox(root)
            stdout = (
                workers.task_dir_for(root, "T-OK") / "worker-stdout.log"
            ).read_text(encoding="utf-8")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(event["terminal_status"], "completed")
        self.assertEqual(event["task_id"], "T-OK")
        self.assertEqual(len(signals), 1)
        self.assertIn("worker ran", stdout)

    def test_supervise_failure_emits_failed_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="failing",
                task_id="T-FAIL",
                prompt_file=prompt,
            )
            result = core.load_object(
                workers.task_dir_for(root, "T-FAIL") / "result.json"
            )
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("exited with code 3", result["failure_reason"])

    def test_supervise_records_prompt_hash_in_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            workers.supervise_worker(
                root,
                worker="echo",
                task_id="T-EV",
                prompt_file=prompt,
            )
            evidence = core.load_object(
                workers.task_dir_for(root, "T-EV") / "evidence.json"
            )
            expected_hash = core.sha256_file(prompt)
        self.assertEqual(evidence["prompt_sha256"], expected_hash)
        self.assertEqual(evidence["worker_config"]["effort"], "high")

    def test_supervise_emits_captured_wake_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            task_dir = workers.task_dir_for(root, "T-WAKE")
            task_dir.mkdir(parents=True)
            wake_target = {
                "schema_version": 1,
                "kind": binding.WAKE_TARGET_KIND,
                "host": "codex",
                "target_thread_id": "thread-origin",
                "codex_command": "/mnt/c/apps/codex.exe",
                "captured_at": "2026-07-08T00:00:00.000+00:00",
            }
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-WAKE",
                    "wake_target": wake_target,
                },
            )
            summary = workers.supervise_worker(
                root,
                worker="echo",
                task_id="T-WAKE",
                prompt_file=prompt,
            )
            event = core.load_object(Path(summary["event_path"]))
            signal = core.inbox(root)[0]
            evidence = core.load_object(task_dir / "evidence.json")
        self.assertEqual(event["wake_target"]["target_thread_id"], "thread-origin")
        self.assertEqual(signal["wake_target"]["target_thread_id"], "thread-origin")
        self.assertEqual(evidence["wake_target"]["target_thread_id"], "thread-origin")

    def test_supervise_can_run_reference_verification_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = workers.workers_config_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"""
[workers.check]
enabled = true
command = ["{sys.executable}", "{CHECK_RUNNER}", "--check-id", "CHECK-1",
           "--label", "unit", "--", "{sys.executable}", "-c", "print('ok')"]
prompt_via = "stdin"
timeout_seconds = 30
""",
                encoding="utf-8",
            )
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="check",
                task_id="T-CHECK",
                prompt_file=prompt,
            )
            verification = core.load_object(
                root
                / ".orchestrator"
                / "checks"
                / "CHECK-1"
                / "verification-result.json"
            )
            worker_stdout = (
                workers.task_dir_for(root, "T-CHECK") / "worker-stdout.log"
            ).read_text(encoding="utf-8")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(verification["kind"], "ORCHESTRATOR_VERIFICATION_RESULT")
        self.assertEqual(verification["status"], "passed")
        self.assertIn("Status: passed", worker_stdout)

    def test_supervise_touches_descriptor_while_worker_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="sleeper",
                task_id="T-LONG",
                prompt_file=prompt,
                heartbeat_interval_seconds=0.2,
            )
            descriptor = core.load_object(
                workers.task_dir_for(root, "T-LONG") / "task.json"
            )
        self.assertEqual(summary["status"], "completed")
        self.assertIn("last_alive_at", descriptor)
        self.assertEqual(descriptor["status"], "completed")

    def test_supervise_kills_worker_after_configured_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="stuck",
                task_id="T-STUCK",
                prompt_file=prompt,
                heartbeat_interval_seconds=0.2,
            )
            result = core.load_object(
                workers.task_dir_for(root, "T-STUCK") / "result.json"
            )
        self.assertEqual(summary["status"], "timed_out")
        self.assertEqual(result["terminal_status"], "timed_out")
        self.assertIn("exceeded", result["failure_reason"])

    def test_invalid_toml_raises_worker_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = workers.workers_config_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not [valid toml", encoding="utf-8")
            with self.assertRaises(workers.WorkerError):
                workers.load_registry(root)


if __name__ == "__main__":
    unittest.main()
