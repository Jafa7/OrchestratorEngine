from __future__ import annotations

import json
import os
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

            config_path = project / ".orchestrator" / "workers.toml"
            config_path.parent.mkdir(parents=True)
            worker_script = (
                "import sys; "
                "sys.stdin.read(); "
                "print('smoke-done')"
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
                    ]
                ),
                encoding="utf-8",
            )
            prompt = root / "smoke-prompt.md"
            prompt.write_text("smoke task\n", encoding="utf-8")

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
            result_path = (
                project
                / ".orchestrator"
                / "tasks"
                / "SMOKE-1"
                / "result.json"
            )
            for _ in range(50):
                if result_path.is_file():
                    break
                time.sleep(0.1)
            self.assertTrue(result_path.is_file())
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

            result = json.loads(result_path.read_text(encoding="utf-8"))
            lines = [
                json.loads(line)
                for line in stream_stdout.splitlines()
                if line.strip()
            ]
        self.assertEqual(bind["host"], "claude")
        self.assertTrue(workers["workers"]["smoke"]["enabled"])
        self.assertEqual(dispatched["status"], "running")
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(inbox[str(project)][0]["task_id"], "SMOKE-1")
        self.assertEqual(stream_stderr, "")
        self.assertEqual(lines[0]["task_id"], "SMOKE-1")

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
