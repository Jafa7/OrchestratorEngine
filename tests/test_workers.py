from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from typing import Any, ClassVar

from orchestrator_engine import binding, core, worker_policy, workers

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_RUNNER = REPO_ROOT / "examples" / "check_runner.py"

# A worker that spawns a descendant of its own: the descendant survives unless
# the whole worker process group is terminated. It prints the descendant pid so
# a test can check that it stopped.
TREE_WORKER = (
    "import subprocess, sys, time; "
    "child = subprocess.Popen("
    "[sys.executable, '-c', 'import time; time.sleep(30)']); "
    "print(child.pid, flush=True); "
    "time.sleep(30)"
)
# Exits on SIGTERM, leaving a marker that proves it was asked before being killed.
GRACEFUL_WORKER = (
    "import signal, sys, time; "
    "signal.signal(signal.SIGTERM, "
    "lambda *unused: (print('graceful stop', flush=True), sys.exit(0))); "
    "time.sleep(30)"
)
# Survives SIGTERM, so only forced escalation can stop it.
STUBBORN_WORKER = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
)

WORKERS_TOML = (
    """
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

[workers.stuck-tree]
enabled = true
command = ["{python}", "-c", "{tree_worker}"]
prompt_via = "stdin"
timeout_seconds = 1

[workers.graceful-stop]
enabled = true
command = ["{python}", "-c", "{graceful_worker}"]
prompt_via = "stdin"
timeout_seconds = 1

[workers.ignores-sigterm]
enabled = true
command = ["{python}", "-c", "{stubborn_worker}"]
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
    .replace("{tree_worker}", TREE_WORKER)
    .replace("{graceful_worker}", GRACEFUL_WORKER)
    .replace("{stubborn_worker}", STUBBORN_WORKER)
)


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


def process_is_running(pid: int) -> bool:
    """Report whether a pid is a live process rather than a reaped or dead one.

    A zombie counts as stopped: it has already been killed and only waits to be
    reaped, so treating it as running would make termination checks flaky.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(") ", 1)[-1].split(" ", 1)[0] != "Z"


def wait_until_stopped(pid: int, *, timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while process_is_running(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def write_prompt(root: Path) -> Path:
    prompt = root / "prompt.md"
    prompt.write_text("do the task", encoding="utf-8")
    return prompt


def write_policy_config(root: Path) -> Path:
    config = workers.workers_config_path(root)
    policy = config.parent / "policies" / "quality.md"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("Use focused checks while editing.\n", encoding="utf-8")
    config.write_text(
        f"""
[policies.quality]
files = ["policies/quality.md"]
quality_priority = "correctness-first"

[workers.capture]
enabled = true
command = ["{sys.executable}", "-c", "import sys; print(sys.stdin.read())"]
prompt_via = "stdin"
expect_long_running = true
policy = "quality"
""",
        encoding="utf-8",
    )
    return policy


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
            warning_codes = [item["code"] for item in validated["warnings"]]
            if profile == "claude-readonly":
                self.assertEqual(
                    warning_codes,
                    ["claude_plan_output_may_be_external"],
                )
            else:
                self.assertEqual(warning_codes, [], profile)

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

    def test_claude_plan_mode_warns_about_external_primary_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                """
[workers.plan]
command = ["claude", "-p", "--permission-mode", "plan"]
prompt_via = "stdin"
""",
                encoding="utf-8",
            )
            listing = workers.list_workers(root)

        codes = {item["code"] for item in listing["workers"]["plan"]["warnings"]}
        self.assertIn("claude_plan_output_may_be_external", codes)

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
        self.assertEqual(
            report["workers"]["long"]["diagnostics"][0]["code"],
            "worker_policy_not_configured",
        )

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

    def test_diagnose_reports_bundled_policy_drift_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            policy = config.parent / "policies" / "quality-efficient.md"
            policy.parent.mkdir(parents=True)
            policy.write_text("locally customized policy\n", encoding="utf-8")
            config.write_text(
                "[policies.quality-efficient]\n"
                'files = ["policies/quality-efficient.md"]\n'
                '[workers.one]\ncommand = ["true"]\n'
                'policy = "quality-efficient"\nexpect_long_running = true\n'
                '[workers.two]\ncommand = ["true"]\n'
                'policy = "quality-efficient"\nexpect_long_running = true\n',
                encoding="utf-8",
            )
            report = workers.diagnose_workers(root)

        bundled = report["policies"]["quality-efficient"]
        self.assertEqual(bundled["status"], "different")
        self.assertEqual(bundled["revision"], 2)
        self.assertEqual(len(bundled["bundled_sha256"]), 64)
        self.assertEqual(len(bundled["local_sha256"]), 64)
        self.assertEqual(
            [item["code"] for item in report["policy_diagnostics"]],
            ["policy_update_available"],
        )

    def test_diagnose_accepts_current_bundled_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            policy = config.parent / "policies" / "quality-efficient.md"
            policy.parent.mkdir(parents=True)
            policy.write_text(
                worker_policy.QUALITY_EFFICIENT_POLICY,
                encoding="utf-8",
            )
            config.write_text(
                "[policies.quality-efficient]\n"
                'files = ["policies/quality-efficient.md"]\n'
                '[workers.one]\ncommand = ["true"]\n'
                'policy = "quality-efficient"\nexpect_long_running = true\n',
                encoding="utf-8",
            )
            report = workers.diagnose_workers(root)

        self.assertEqual(
            report["policies"]["quality-efficient"]["status"],
            "current",
        )
        self.assertEqual(report["policy_diagnostics"], [])

    def test_strict_ai_profile_warns_without_verification_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                "[dispatch]\n"
                'intent_enforcement = "strict"\n'
                "[workers.claude]\n"
                'command = ["claude", "-p"]\n'
                'prompt_via = "stdin"\n'
                "[workers.claude.admission]\n"
                'roles = ["implementation"]\n',
                encoding="utf-8",
            )
            report = workers.diagnose_workers(root, worker="claude")

        codes = {
            item["code"]
            for item in report["workers"]["claude"]["diagnostics"]
        }
        self.assertIn("strict_ai_verification_not_declared", codes)

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

    def test_registry_rejects_unknown_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                '[workers.worker]\ncommand = ["true"]\npolicy = "missing"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(workers.WorkerError, "unknown policy"):
                workers.load_registry(root)

    def test_list_workers_reports_selected_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = write_policy_config(root)
            listing = workers.list_workers(root)

        worker = listing["workers"]["capture"]
        self.assertEqual(worker["policy"], "quality")
        self.assertEqual(worker["policy_files"], [str(policy.resolve())])
        self.assertEqual(
            worker["policy_metadata"]["quality_priority"],
            "correctness-first",
        )

    def test_diagnose_reports_unreadable_selected_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = write_policy_config(root)
            policy.unlink()
            report = workers.diagnose_workers(root, worker="capture")

        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(
            report["workers"]["capture"]["diagnostics"][0]["code"],
            "worker_policy_unreadable",
        )

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
        self.assertEqual(descriptor["status"], "starting")
        self.assertEqual(descriptor["supervisor_pid"], 5150)
        self.assertIn("supervise", FakePopen.command)
        self.assertTrue(FakePopen.kwargs["start_new_session"])

    def test_run_worker_leaves_task_identity_to_the_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            descriptor = workers.run_worker(
                root,
                worker="echo",
                task_id="T-HANDOFF",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            stored = core.load_object(Path(descriptor["descriptor_path"]))
        self.assertEqual(stored["status"], "starting")
        self.assertNotIn("supervisor_pid", stored)
        self.assertNotIn("worker_pid", stored)

    def test_run_worker_does_not_rewrite_the_descriptor_after_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            descriptor_path = workers.task_dir_for(root, "T-RACE") / "task.json"

            class FinalizingPopen:
                """A supervisor that finishes the task before dispatch returns.

                Any post-spawn descriptor write by the dispatcher would resurrect
                this finished task as `running` and drop its terminal fields.
                """

                finalized: ClassVar[bytes] = b""

                def __init__(self, command: list[str], **kwargs: object) -> None:
                    self.pid = 6001
                    core.atomic_json(
                        descriptor_path,
                        {
                            "schema_version": 1,
                            "kind": workers.TASK_KIND,
                            "task_id": "T-RACE",
                            "worker": "echo",
                            "status": "completed",
                            "prompt_file": str(prompt),
                            "task_dir": str(descriptor_path.parent),
                            "created_at": "2026-07-12T00:00:00.000+00:00",
                            "finished_at": "2026-07-12T00:00:01.000+00:00",
                            "supervisor_pid": 6001,
                            "event_path": "/events/event.json",
                            "signal_path": "/signals/signal.json",
                        },
                    )
                    self.__class__.finalized = descriptor_path.read_bytes()

            workers.run_worker(
                root,
                worker="echo",
                task_id="T-RACE",
                prompt_file=prompt,
                popen_factory=FinalizingPopen,
            )
            after_dispatch = descriptor_path.read_bytes()
            stored = core.load_object(descriptor_path)

        self.assertEqual(after_dispatch, FinalizingPopen.finalized)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["finished_at"], "2026-07-12T00:00:01.000+00:00")
        self.assertEqual(stored["event_path"], "/events/event.json")

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

    def test_run_worker_snapshots_policy_before_supervisor_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_policy_config(root)
            prompt = write_prompt(root)
            descriptor = workers.run_worker(
                root,
                worker="capture",
                task_id="T-POLICY",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            effective = Path(descriptor["effective_prompt_file"])
            content = effective.read_text(encoding="utf-8")
            effective_hash = core.sha256_file(effective)

        self.assertIn("Use focused checks while editing.", content)
        self.assertIn("do the task", content)
        self.assertEqual(descriptor["worker_policy"]["name"], "quality")
        self.assertEqual(
            descriptor["effective_prompt_sha256"],
            effective_hash,
        )

    def test_run_worker_releases_task_claim_when_policy_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = write_policy_config(root)
            prompt = write_prompt(root)
            policy.unlink()

            with self.assertRaisesRegex(workers.WorkerError, "unreadable"):
                workers.run_worker(
                    root,
                    worker="capture",
                    task_id="T-POLICY-FAIL",
                    prompt_file=prompt,
                    popen_factory=FakePopen,
                )

            self.assertFalse(
                (workers.task_dir_for(root, "T-POLICY-FAIL") / "task.json").exists()
            )


class WorkerSuperviseTests(unittest.TestCase):
    def test_supervise_uses_task_snapshot_without_source_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            dispatched = workers.run_worker(
                root,
                worker="echo",
                task_id="T-SNAPSHOT",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            prompt.unlink()
            summary = workers.supervise_worker(
                root,
                worker="echo",
                task_id="T-SNAPSHOT",
                prompt_file=prompt,
            )
            evidence = core.load_object(
                workers.task_dir_for(root, "T-SNAPSHOT") / "evidence.json"
            )

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(evidence["prompt_sha256"], dispatched["prompt_sha256"])
        self.assertIsNone(evidence["worker_config"]["policy"])

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

    def test_supervise_uses_dispatch_snapshot_after_policy_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = write_policy_config(root)
            prompt = write_prompt(root)
            dispatched = workers.run_worker(
                root,
                worker="capture",
                task_id="T-POLICY",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            policy.write_text("CHANGED POLICY\n", encoding="utf-8")
            prompt.unlink()
            workers.supervise_worker(
                root,
                worker="capture",
                task_id="T-POLICY",
                prompt_file=prompt,
            )
            task_dir = workers.task_dir_for(root, "T-POLICY")
            stdout = (task_dir / "worker-stdout.log").read_text(encoding="utf-8")
            evidence = core.load_object(task_dir / "evidence.json")

        self.assertIn("Use focused checks while editing.", stdout)
        self.assertIn("do the task", stdout)
        self.assertNotIn("CHANGED POLICY", stdout)
        self.assertEqual(
            evidence["prompt_sha256"],
            dispatched["prompt_sha256"],
        )
        self.assertEqual(
            evidence["effective_prompt_sha256"],
            dispatched["effective_prompt_sha256"],
        )
        self.assertEqual(evidence["worker_policy"]["name"], "quality")

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
                termination_grace_seconds=0.5,
            )
            result = core.load_object(
                workers.task_dir_for(root, "T-STUCK") / "result.json"
            )
        self.assertEqual(summary["status"], "timed_out")
        self.assertEqual(result["terminal_status"], "timed_out")
        self.assertIn("exceeded", result["failure_reason"])

    def test_supervise_claims_the_descriptor_before_starting_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            descriptor_path = workers.task_dir_for(root, "T-CLAIM") / "task.json"
            claims: list[dict[str, Any]] = []

            def recording_popen(command: list[str], **kwargs: Any) -> Any:
                claims.append(core.load_object(descriptor_path))
                return subprocess.Popen(command, **kwargs)

            workers.run_worker(
                root,
                worker="echo",
                task_id="T-CLAIM",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            summary = workers.supervise_worker(
                root,
                worker="echo",
                task_id="T-CLAIM",
                prompt_file=prompt,
                popen_factory=recording_popen,
            )
            stored = core.load_object(descriptor_path)

        self.assertEqual(claims[0]["status"], "running")
        self.assertEqual(claims[0]["supervisor_pid"], os.getpid())
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(stored["supervisor_pid"], os.getpid())
        # The worker leads its own process group, so its group id is its pid.
        self.assertEqual(stored["worker_pgid"], stored["worker_pid"])

    def test_supervise_timeout_terminates_the_worker_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="stuck-tree",
                task_id="T-TREE",
                prompt_file=prompt,
                heartbeat_interval_seconds=0.2,
                termination_grace_seconds=0.5,
            )
            task_dir = workers.task_dir_for(root, "T-TREE")
            result = core.load_object(task_dir / "result.json")
            descriptor = core.load_object(task_dir / "task.json")
            descendant_pid = int(
                (task_dir / "worker-stdout.log").read_text(encoding="utf-8").strip()
            )
            descendant_stopped = wait_until_stopped(descendant_pid)
            worker_stopped = wait_until_stopped(descriptor["worker_pid"])

        self.assertEqual(summary["status"], "timed_out")
        self.assertEqual(result["termination"]["scope"], "process_group")
        self.assertEqual(
            result["termination"]["process_group"],
            descriptor["worker_pgid"],
        )
        self.assertTrue(worker_stopped)
        # The worker's own child must not outlive the terminated task.
        self.assertTrue(descendant_stopped)

    def test_supervise_timeout_stops_the_worker_gracefully_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="graceful-stop",
                task_id="T-GRACE",
                prompt_file=prompt,
                heartbeat_interval_seconds=0.2,
                termination_grace_seconds=5.0,
            )
            task_dir = workers.task_dir_for(root, "T-GRACE")
            result = core.load_object(task_dir / "result.json")
            stdout = (task_dir / "worker-stdout.log").read_text(encoding="utf-8")

        self.assertEqual(summary["status"], "timed_out")
        self.assertIn("graceful stop", stdout)
        self.assertFalse(result["termination"]["escalated"])
        self.assertEqual(result["termination"]["signals"][0]["signal"], "SIGTERM")

    def test_supervise_timeout_escalates_when_sigterm_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="ignores-sigterm",
                task_id="T-FORCE",
                prompt_file=prompt,
                heartbeat_interval_seconds=0.2,
                termination_grace_seconds=0.3,
            )
            task_dir = workers.task_dir_for(root, "T-FORCE")
            result = core.load_object(task_dir / "result.json")
            descriptor = core.load_object(task_dir / "task.json")
            worker_stopped = wait_until_stopped(descriptor["worker_pid"])

        self.assertEqual(summary["status"], "timed_out")
        self.assertTrue(result["termination"]["escalated"])
        self.assertTrue(result["termination"]["exited"])
        self.assertEqual(
            [entry["signal"] for entry in result["termination"]["signals"]],
            ["SIGTERM", "SIGKILL"],
        )
        self.assertTrue(worker_stopped)

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
