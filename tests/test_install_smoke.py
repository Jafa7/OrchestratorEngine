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

            config_path = project / ".orchestrator" / "workers.toml"
            config_path.parent.mkdir(parents=True)
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
                        "[workers.smoke]",
                        "enabled = true",
                        f"command = [{json.dumps(str(python))}, "
                        f"\"-c\", {json.dumps(worker_script)}]",
                        'prompt_via = "stdin"',
                        "timeout_seconds = 10",
                        "",
                        "[workers.failing]",
                        "enabled = true",
                        f"command = [{json.dumps(str(python))}, "
                        f"\"-c\", {json.dumps(failing_script)}]",
                        'prompt_via = "stdin"',
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
                        "timeout_seconds = 30",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            prompt = root / "smoke-prompt.md"
            prompt.write_text("smoke task\n", encoding="utf-8")

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
        self.assertEqual(bind["host"], "claude")
        self.assertIn("0.1.0", version)
        self.assertTrue(workers["workers"]["smoke"]["enabled"])
        self.assertEqual(dispatched["status"], "running")
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(failed_result["terminal_status"], "failed")
        self.assertEqual(check_result["terminal_status"], "completed")
        self.assertEqual(verification["status"], "passed")
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
