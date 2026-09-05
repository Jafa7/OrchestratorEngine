from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
import unittest
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
with (REPO_ROOT / "pyproject.toml").open("rb") as project_file:
    PROJECT_VERSION = tomllib.load(project_file)["project"]["version"]


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

            fake_gh = root / "GitHub CLI" / "gh"
            fake_gh.parent.mkdir()
            fake_gh.write_text(
                "\n".join(
                    [
                        f"#!{python}",
                        "import json",
                        "import sys",
                        "if sys.argv[1:3] == ['run', 'view']:",
                        "    print(json.dumps({",
                        "        'attempt': 1, 'conclusion': 'success',",
                        "        'createdAt': '2026-09-05T10:00:00Z',",
                        "        'databaseId': 123, 'event': 'push',",
                        "        'headBranch': 'main',",
                        "        'headSha': 'abcdef123456',",
                        "        'startedAt': '2026-09-05T10:00:01Z',",
                        "        'status': 'completed',",
                        "        'updatedAt': '2026-09-05T10:01:00Z',",
                        (
                            "        'url': 'https://github.com/Example/Project/"
                            "actions/runs/123',"
                        ),
                        "        'workflowDatabaseId': 7,",
                        "        'workflowName': 'CI'",
                        "    }))",
                        "    raise SystemExit(0)",
                        "if sys.argv[1:3] == ['pr', 'view']:",
                        "    print(json.dumps({",
                        "        'number': 7, 'state': 'OPEN', 'isDraft': False,",
                        (
                            "        'headRefOid': "
                            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                        ),
                        "        'reviewDecision': 'APPROVED',",
                        "        'mergeable': 'MERGEABLE',",
                        "        'statusCheckRollup': [],",
                        "        'url': 'https://github.com/Example/Project/pull/7'",
                        "    }))",
                        "    raise SystemExit(0)",
                        "raise SystemExit(9)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

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
            host_capabilities = self.run_cli(cli, project, "host-capabilities")
            runtime_capabilities = self.run_cli(
                cli, project, "runtime-capabilities"
            )
            conformance = self.run_cli(
                cli,
                project,
                "conformance",
                "run",
                "--mode",
                "full",
                "--timeout-seconds",
                "15",
            )

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
            integrations_path = project / ".orchestrator" / "integrations.toml"
            integrations_path.write_text(
                "\n".join(
                    [
                        "[integrations.github_actions]",
                        "enabled = true",
                        f"gh_command = {json.dumps(str(fake_gh))}",
                        'allowed_repositories = ["Example/Project"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            checks_config = project / ".orchestrator" / "checks.toml"
            checks_config.write_text(
                "\n".join(
                    [
                        "[suites.smoke]",
                        'verification = "focused"',
                        "expected_duration_seconds = 1",
                        "",
                        "[[suites.smoke.commands]]",
                        'label = "installed-cli"',
                        f"argv = [{json.dumps(str(python))}, "
                        '"-c", "print(\'first-class-check-ok\')"]',
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

            def wait_terminal_descriptor(path: Path) -> dict:
                for _ in range(100):
                    if path.is_file():
                        value = json.loads(path.read_text(encoding="utf-8"))
                        if value.get("status") not in {"starting", "running"}:
                            return value
                    time.sleep(0.1)
                self.fail(f"descriptor did not become terminal: {path}")

            bind = self.run_cli(cli, project, "bind", "--host", "claude")
            workstream_start = self.run_cli(
                cli,
                project,
                "workstream",
                "start",
                "--workstream-id",
                "INSTALL-WORKSTREAM",
                "--goal",
                "Exercise the installed checkpoint contract.",
            )
            workstream_checkpoint = self.run_cli(
                cli,
                project,
                "workstream",
                "checkpoint",
                "--workstream-id",
                "INSTALL-WORKSTREAM",
                "--checkpoint-id",
                "phase-1",
                "--decision",
                "paused",
                "--summary",
                "Install smoke checkpoint complete.",
            )
            workstream_status = self.run_cli(
                cli,
                project,
                "workstream",
                "status",
                "--workstream-id",
                "INSTALL-WORKSTREAM",
            )
            local_check_plan = self.run_cli(
                cli,
                project,
                "check",
                "plan",
                "--suite",
                "smoke",
            )
            local_check_run = self.run_cli(
                cli,
                project,
                "check",
                "run",
                "--check-id",
                "INSTALL-FIRST-CLASS",
                "--suite",
                "smoke",
                "--execution",
                "foreground",
                "--wake-policy",
                "never",
            )
            local_check_status = self.run_cli(
                cli,
                project,
                "check",
                "status",
                "--check-id",
                "INSTALL-FIRST-CLASS",
            )
            local_check_reap = self.run_cli(
                cli,
                project,
                "check",
                "reap",
                "--check-id",
                "INSTALL-FIRST-CLASS",
            )
            ci_monitor = self.run_cli(
                cli,
                project,
                "ci",
                "watch",
                "--repo",
                "Example/Project",
                "--run-id",
                "123",
                "--expected-head-sha",
                "abcdef1",
            )
            wait_terminal_descriptor(Path(ci_monitor["monitor_dir"]) / "monitor.json")
            ci_status = self.run_cli(
                cli,
                project,
                "ci",
                "status",
                "--monitor-id",
                ci_monitor["monitor_id"],
            )
            ci_reap = self.run_cli(cli, project, "ci", "reap")
            pr_monitor = self.run_cli(
                cli,
                project,
                "pr",
                "watch",
                "--repo",
                "Example/Project",
                "--pr-number",
                "7",
                "--expected-head-sha",
                "a" * 40,
                "--review-policy",
                "approved",
            )
            wait_terminal_descriptor(Path(pr_monitor["monitor_dir"]) / "monitor.json")
            pr_status = self.run_cli(
                cli,
                project,
                "pr",
                "status",
                "--monitor-id",
                pr_monitor["monitor_id"],
            )
            pr_reap = self.run_cli(cli, project, "pr", "reap")
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
            worker_wait_help = subprocess.run(
                [str(cli), "worker", "wait", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            ci_watch_help = subprocess.run(
                [str(cli), "ci", "watch", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            pr_watch_help = subprocess.run(
                [str(cli), "pr", "watch", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            workstream_checkpoint_help = subprocess.run(
                [str(cli), "workstream", "checkpoint", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            local_check_help = subprocess.run(
                [str(cli), "check", "run", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            artifact_resolve_help = subprocess.run(
                [str(cli), "artifact", "resolve", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            upgrade_check_help = subprocess.run(
                [str(cli), "upgrade", "check", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            policy_export_help = subprocess.run(
                [str(cli), "worker", "policy", "export", "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            ).stdout
            exported_policy = root / "exported-quality-efficient.md"
            policy_export = self.run_cli(
                cli,
                project,
                "worker",
                "policy",
                "export",
                "--name",
                "quality-efficient",
                "--output",
                str(exported_policy),
            )
            upgrade_check = self.run_cli(cli, project, "upgrade", "check")
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
            group_wait_status = self.run_cli(
                cli,
                project,
                "worker",
                "wait",
                "--task-id",
                "SMOKE-1",
                "--task-id",
                "SMOKE-CHECK",
                "--mode",
                "all",
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
            wait_terminal_descriptor(
                project
                / ".orchestrator"
                / "tasks"
                / "SMOKE-CHECK"
                / "task.json"
            )
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
            exported_policy_exists = exported_policy.is_file()
        self.assertEqual(bind["host"], "claude")
        self.assertEqual(workstream_start["status"], "active")
        self.assertEqual(workstream_checkpoint["decision"], "paused")
        self.assertEqual(workstream_status["status_counts"], {"paused": 1})
        self.assertEqual(local_check_plan["recommended_execution"], "foreground")
        self.assertEqual(local_check_run["status"], "passed")
        self.assertEqual(local_check_status["status_counts"], {"passed": 1})
        self.assertEqual(local_check_reap["reaped_count"], 0)
        self.assertEqual(version, f"orchestrator-engine {PROJECT_VERSION}")
        codex_capability = next(
            item
            for item in host_capabilities["hosts"]
            if item["host"] == "codex"
        )
        self.assertEqual(codex_capability["delivery_mode"], "session_queue")
        self.assertEqual(codex_capability["requirement"], "codex queue")
        self.assertEqual(runtime_capabilities["portable_core"], "supported")
        self.assertEqual(runtime_capabilities["detached_lifecycle"], "supported")
        self.assertEqual(conformance["kind"], "ORCHESTRATOR_CONFORMANCE_REPORT")
        self.assertEqual(conformance["status"], "passed")
        self.assertEqual(conformance["effective_mode"], "full")
        self.assertEqual(conformance["fixture"]["status"], "removed")
        self.assertEqual(adoption["kind"], "ORCHESTRATOR_ADOPTION")
        self.assertTrue(policy_exists)
        self.assertTrue(workers["workers"]["smoke"]["enabled"])
        self.assertEqual(worker_diagnostics["kind"], "WORKER_DIAGNOSTICS")
        self.assertEqual(worker_diagnostics["diagnostic_count"], 0)
        self.assertIn("--availability-mode", worker_run_help)
        self.assertIn("--mode", worker_wait_help)
        self.assertIn("--expected-head-sha", ci_watch_help)
        self.assertIn("--wake-policy", ci_watch_help)
        self.assertIn("--expected-head-sha", pr_watch_help)
        self.assertIn("--review-policy", pr_watch_help)
        self.assertIn("--ready", workstream_checkpoint_help)
        self.assertIn("--execution", local_check_help)
        self.assertIn("--path", artifact_resolve_help)
        self.assertIn("--reason", artifact_resolve_help)
        self.assertIn("--strict", upgrade_check_help)
        self.assertIn("--replace", policy_export_help)
        self.assertEqual(
            policy_export["kind"],
            "ORCHESTRATOR_BUNDLED_POLICY_EXPORT",
        )
        self.assertTrue(exported_policy_exists)
        self.assertEqual(upgrade_check["kind"], "ORCHESTRATOR_UPGRADE_CHECK")
        # Dispatch hands the descriptor to the supervisor, which claims it and
        # records `running` itself; the dispatcher never writes it again.
        self.assertEqual(dispatched["status"], "starting")
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(wait_status["kind"], "WORKER_WAIT_STATUS")
        self.assertEqual(wait_status["status"], "completed")
        self.assertEqual(group_wait_status["kind"], "WORKER_WAIT_GROUP_STATUS")
        self.assertEqual(group_wait_status["terminal_count"], 2)
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
        self.assertEqual(ci_status["kind"], "GITHUB_ACTIONS_MONITOR_STATUS")
        self.assertEqual(ci_status["monitors"][0]["ci_conclusion"], "success")
        self.assertEqual(ci_reap["kind"], "GITHUB_ACTIONS_MONITOR_REAP_REPORT")
        self.assertEqual(ci_reap["reaped_count"], 0)
        self.assertEqual(pr_status["kind"], "GITHUB_PR_READINESS_STATUS")
        self.assertEqual(pr_status["monitors"][0]["status"], "ready")
        self.assertEqual(pr_reap["reaped_count"], 0)
        self.assertIn(aggregate_status_result.returncode, {0, 2})
        self.assertEqual(aggregate_status["kind"], "ORCHESTRATOR_STATUS_REPORT")
        self.assertIn("worker_tasks", aggregate_status["components"])
        self.assertIn("[runtime-report][InstallSmoke]", report_draft_text)
        inbox_task_ids = {row.get("task_id") for row in inbox[str(project)]}
        inbox_operation_ids = {
            row.get("operation_id") for row in inbox[str(project)]
        }
        self.assertIn("SMOKE-1", inbox_task_ids)
        self.assertIn("SMOKE-FAIL", inbox_task_ids)
        self.assertIn("SMOKE-CHECK", inbox_task_ids)
        self.assertIn(ci_monitor["monitor_id"], inbox_operation_ids)
        self.assertIn(pr_monitor["monitor_id"], inbox_operation_ids)
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
