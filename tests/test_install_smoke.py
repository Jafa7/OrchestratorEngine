from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


class InstallSmokeTests(unittest.TestCase):
    def test_installed_cli_runs_worker_end_to_end_without_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            venv_dir = root / "venv"
            venv.EnvBuilder(with_pip=True).create(venv_dir)
            python = venv_dir / "bin" / "python"
            cli = venv_dir / "bin" / "orchestrator-engine"
            project = root / "adopted-project"
            project.mkdir()

            env = clean_env()
            subprocess.run(
                [str(python), "-m", "pip", "install", str(REPO_ROOT)],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            version = subprocess.run(
                [str(cli), "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            ).stdout.strip()

            adoption = self.run_cli(cli, project, "adopt", "--host", "claude")

            config_path = project / ".orchestrator" / "workers.toml"
            policy_path = (
                project
                / ".orchestrator"
                / "policies"
                / "quality-efficient.md"
            )
            scripts = project / "scripts"
            scripts.mkdir()
            check_runner = scripts / "orchestrator_check_runner.py"
            shutil.copyfile(REPO_ROOT / "examples" / "check_runner.py", check_runner)
            worker_script = (
                "import sys; "
                "sys.stdin.read(); "
                "print('smoke-done')"
            )
            failing_script = (
                "import sys; "
                "sys.stdin.read(); "
                "print('fail-now'); "
                "sys.exit(7)"
            )
            config_path.write_text(
                "\n".join(
                    [
                        "[policies.quality-efficient]",
                        'files = ["policies/quality-efficient.md"]',
                        'quality_priority = "correctness-first"',
                        "",
                        "[dispatch]",
                        'intent_enforcement = "strict"',
                        "",
                        "[workers.smoke]",
                        "enabled = true",
                        f"command = [{json.dumps(str(python))}, "
                        f"\"-c\", {json.dumps(worker_script)}]",
                        'prompt_via = "stdin"',
                        'policy = "quality-efficient"',
                        'permission_profile = "full"',
                        f"availability_probe = [{json.dumps(str(python))}, "
                        '"-c", "raise SystemExit(0)"]',
                        "availability_timeout_seconds = 5",
                        "timeout_seconds = 10",
                        "",
                        "[workers.smoke.admission]",
                        'roles = ["implementation"]',
                        'max_risk = "high"',
                        'verification = ["full"]',
                        (
                            "authorizations = { commit = false, push = false, "
                            "network = false }"
                        ),
                        "",
                        "[workers.failing]",
                        "enabled = true",
                        f"command = [{json.dumps(str(python))}, "
                        f"\"-c\", {json.dumps(failing_script)}]",
                        'prompt_via = "stdin"',
                        'policy = "quality-efficient"',
                        "timeout_seconds = 10",
                        "",
                        "[workers.check]",
                        "enabled = true",
                        f"command = [{json.dumps(str(python))}, "
                        f"{json.dumps(str(check_runner))}, "
                        f'"--project-root", {json.dumps(str(project))}, '
                        '"--check-id", "INSTALL-CHECK", '
                        '"--label", "inline", "--", '
                        f"{json.dumps(str(python))}, "
                        '"-c", "print(\'check-ok\')"]',
                        'prompt_via = "stdin"',
                        'policy = "quality-efficient"',
                        "timeout_seconds = 30",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            prompt = root / "smoke-prompt.md"
            prompt.write_text("smoke task\n", encoding="utf-8")
            intent = root / "smoke-intent.json"
            intent.write_text(
                json.dumps(
                    {
                        "role": "implementation",
                        "risk": "low",
                        "verification": "full",
                        "permissions": "full",
                        "authorizations": {
                            "commit": False,
                            "push": False,
                            "network": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            def wait_result(task_id: str) -> dict:
                path = (
                    project
                    / ".orchestrator"
                    / "tasks"
                    / task_id
                    / "result.json"
                )
                for _ in range(50):
                    if path.is_file():
                        return json.loads(path.read_text(encoding="utf-8"))
                    time.sleep(0.1)
                self.fail(f"missing result for {task_id}")

            def wait_file(path: Path) -> None:
                for _ in range(50):
                    if path.is_file():
                        return
                    time.sleep(0.1)
                self.fail(f"missing file: {path}")

            bind = self.run_cli(cli, project, "bind", "--host", "claude")
            workers = self.run_cli(cli, project, "worker", "list")
            worker_diagnostics = self.run_cli(
                cli,
                project,
                "worker",
                "diagnose",
                "--enabled-only",
            )
            worker_run_help = subprocess.run(
                [str(cli), "worker", "run", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            dispatched = self.run_cli(
                cli,
                project,
                "worker",
                "run",
                "--worker",
                "smoke",
                "--task-id",
                "SMOKE-1",
                "--prompt-file",
                str(prompt),
                "--availability-mode",
                "require-available",
                "--intent-file",
                str(intent),
            )
            self.run_cli(
                cli,
                project,
                "worker",
                "run",
                "--worker",
                "failing",
                "--task-id",
                "SMOKE-FAIL",
                "--prompt-file",
                str(prompt),
            )
            self.run_cli(
                cli,
                project,
                "worker",
                "run",
                "--worker",
                "check",
                "--task-id",
                "SMOKE-CHECK",
                "--prompt-file",
                str(prompt),
            )
            result = wait_result("SMOKE-1")
            failed_result = wait_result("SMOKE-FAIL")
            check_result = wait_result("SMOKE-CHECK")
            wait_status = self.run_cli(
                cli,
                project,
                "worker",
                "wait",
                "--task-id",
                "SMOKE-1",
                "--json",
            )
            smoke_evidence = json.loads(
                (
                    project
                    / ".orchestrator"
                    / "tasks"
                    / "SMOKE-1"
                    / "evidence.json"
                ).read_text(encoding="utf-8")
            )
            task_diagnostics = self.run_cli(
                cli,
                project,
                "worker",
                "tasks",
                "--severity",
                "error",
            )
            inbox = self.run_cli(cli, project, "inbox")
            stream_process = subprocess.Popen(
                [
                    str(cli),
                    "--project-root",
                    str(project),
                    "watcher",
                    "--state-file",
                    str(root / "watcher-state.json"),
                    "stream",
                    "--interval-seconds",
                    "0.1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                stream_stdout, stream_stderr = stream_process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                stream_process.terminate()
                stream_stdout, stream_stderr = stream_process.communicate(timeout=5)

            lines = [
                json.loads(line)
                for line in stream_stdout.splitlines()
                if line.strip()
            ]
            self.run_cli(cli, project, "bind", "--host", "codex", "--thread-id", "t")
            service_start = self.run_cli(
                cli,
                project,
                "watcher",
                "--host",
                "codex",
                "--action",
                "callback",
                "service",
                "start",
                "--interval-seconds",
                "0.5",
            )
            try:
                service_status = self.run_cli(
                    cli,
                    project,
                    "watcher",
                    "--host",
                    "codex",
                    "--action",
                    "callback",
                    "service",
                    "status",
                )
            finally:
                service_stop = self.run_cli(
                    cli,
                    project,
                    "watcher",
                    "--host",
                    "codex",
                    "--action",
                    "callback",
                    "service",
                    "stop",
                )
            check_file = project / ".orchestrator" / "checks" / "INSTALL-CHECK"
            wait_file(check_file / "verification-result.json")
            verification = json.loads(
                (check_file / "verification-result.json").read_text(
                    encoding="utf-8"
                )
            )
            checks_status = self.run_cli(cli, project, "checks")
            aggregate_status_result = subprocess.run(
                [str(cli), "--project-root", str(project), "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=clean_env(),
            )
            aggregate_status = json.loads(aggregate_status_result.stdout)
            report_draft = project / "orchestrator-report.md"
            subprocess.run(
                [
                    str(cli),
                    "--project-root",
                    str(project),
                    "report",
                    "draft",
                    "--project-name",
                    "InstallSmoke",
                    "--output",
                    str(report_draft),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                env=clean_env(),
            )
            report_draft_text = report_draft.read_text(encoding="utf-8")
            policy_exists = policy_path.is_file()
        self.assertEqual(bind["host"], "claude")
        self.assertEqual(version, "orchestrator-engine 0.3.0")
        self.assertEqual(adoption["kind"], "ORCHESTRATOR_ADOPTION")
        self.assertTrue(policy_exists)
        self.assertTrue(workers["workers"]["smoke"]["enabled"])
        self.assertEqual(worker_diagnostics["kind"], "WORKER_DIAGNOSTICS")
        self.assertEqual(worker_diagnostics["diagnostic_count"], 0)
        self.assertIn("--availability-mode", worker_run_help)
        # Dispatch hands the descriptor to the supervisor, which claims it and
        # records `running` itself; the dispatcher never writes it again.
        self.assertEqual(dispatched["status"], "starting")
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(wait_status["kind"], "WORKER_WAIT_STATUS")
        self.assertEqual(wait_status["status"], "completed")
        self.assertEqual(
            smoke_evidence["worker_policy"]["name"],
            "quality-efficient",
        )
        self.assertEqual(
            smoke_evidence["availability_preflight"]["status"],
            "available",
        )
        self.assertEqual(smoke_evidence["intent_admission"]["mode"], "strict")
        self.assertEqual(failed_result["terminal_status"], "failed")
        self.assertEqual(check_result["terminal_status"], "completed")
        self.assertEqual(task_diagnostics["kind"], "WORKER_TASK_DIAGNOSTICS")
        self.assertEqual(task_diagnostics["diagnostic_count"], 0)
        self.assertEqual(verification["status"], "passed")
        self.assertEqual(checks_status["kind"], "ORCHESTRATOR_CHECKS_STATUS")
        self.assertEqual(checks_status["checks"]["INSTALL-CHECK"]["status"], "passed")
        self.assertIn(aggregate_status_result.returncode, {0, 2})
        self.assertEqual(aggregate_status["kind"], "ORCHESTRATOR_STATUS_REPORT")
        self.assertIn("worker_tasks", aggregate_status["components"])
        self.assertIn("[runtime-report][InstallSmoke]", report_draft_text)
        inbox_task_ids = {row["task_id"] for row in inbox[str(project)]}
        self.assertIn("SMOKE-1", inbox_task_ids)
        self.assertIn("SMOKE-FAIL", inbox_task_ids)
        self.assertIn("SMOKE-CHECK", inbox_task_ids)
        self.assertEqual(stream_stderr, "")
        stream_task_ids = {line["task_id"] for line in lines}
        self.assertTrue(stream_task_ids & {"SMOKE-1", "SMOKE-FAIL", "SMOKE-CHECK"})
        self.assertEqual(service_start["host_filter"], ["codex"])
        self.assertIn("codex", service_status["host_filter"])
        self.assertEqual(service_stop["status"], "stopped")

    def run_cli(self, cli: Path, project: Path, *args: str) -> dict:
        completed = subprocess.run(
            [str(cli), "--project-root", str(project), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=clean_env(),
        )
        return json.loads(completed.stdout)
