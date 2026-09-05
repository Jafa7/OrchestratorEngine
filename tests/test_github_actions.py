from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import (
    binding,
    codex_app,
    core,
    github_actions,
    platform_runtime,
    verification,
    wakeup,
    watcher,
    worker_lease,
)


def write_config(root: Path, *, gh_command: str = "gh") -> None:
    path = core.state_root(root) / github_actions.CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "[integrations.github_actions]",
                "enabled = true",
                f"gh_command = {json.dumps(gh_command)}",
                'allowed_repositories = ["Example/Project"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def gh_view(
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    run_id: int = 123,
    attempt: int = 1,
    head_sha: str = "abcdef1234567890",
) -> dict[str, object]:
    return {
        "attempt": attempt,
        "conclusion": conclusion,
        "createdAt": "2026-09-05T10:00:00Z",
        "databaseId": run_id,
        "event": "push",
        "headBranch": "main",
        "headSha": head_sha,
        "startedAt": "2026-09-05T10:00:01Z",
        "status": status,
        "updatedAt": "2026-09-05T10:01:00Z",
        "url": "https://github.com/Example/Project/actions/runs/123",
        "workflowDatabaseId": 7,
        "workflowName": "CI",
    }


def completed_view(value: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(value).encode(),
        stderr=b"",
    )


class DummyProcess:
    pid = 4321


class RunningProcess:
    pid = 4322

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stderr = io.BytesIO(b"")

    def poll(self) -> int | None:
        return self.returncode


class GitHubActionsTests(unittest.TestCase):
    def test_watch_fails_before_artifacts_when_detached_lifecycle_unsupported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                mock.patch.object(
                    platform_runtime,
                    "detached_lifecycle_supported",
                    return_value=False,
                ),
                self.assertRaises(platform_runtime.PlatformRuntimeError),
            ):
                github_actions.start_monitor(
                    root,
                    repository="Example/Project",
                    run_id=123,
                )

            self.assertFalse(
                github_actions.monitor_root(root, state_dir=".orchestrator").exists()
            )

    def test_followup_event_identity_has_unambiguous_components(self) -> None:
        project = Path("/tmp/example-project")
        first = core.followup_event_id(
            project,
            source_kind="source/with-slash",
            operation_id="operation",
        )
        second = core.followup_event_id(
            project,
            source_kind="source",
            operation_id="with-slash/operation",
        )

        self.assertNotEqual(first, second)

    def test_start_is_detached_allowlisted_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            binding.write_binding(
                root,
                host="codex",
                target_thread_id="thread-1",
            )
            popen = mock.Mock(return_value=DummyProcess())
            first = github_actions.start_monitor(
                root,
                repository="Example/Project",
                run_id="123",
                expected_head_sha="abcdef1",
                popen_factory=popen,
            )
            second = github_actions.start_monitor(
                root,
                repository="example/project",
                run_id="123",
                expected_head_sha="abcdef1",
                popen_factory=popen,
            )
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "different dispatch options",
            ):
                github_actions.start_monitor(
                    root,
                    repository="Example/Project",
                    run_id="123",
                    expected_head_sha="9999999",
                    popen_factory=popen,
                )
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "active monitor",
            ):
                github_actions.start_monitor(
                    root,
                    repository="Example/Project",
                    run_id="123",
                    expected_head_sha="abcdef1",
                    monitor_id="explicit-retry",
                    popen_factory=popen,
                )
            self.assertFalse(
                github_actions.monitor_dir_for(root, "explicit-retry").exists()
            )
            launch = core.load_object(
                github_actions.supervisor_launch_path(
                    github_actions.monitor_dir_for(root, first["monitor_id"])
                )
            )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["wake_target"]["target_thread_id"], "thread-1")
        self.assertEqual(popen.call_count, 1)
        command = popen.call_args.args[0]
        self.assertIn("supervise", command)
        self.assertEqual(command.count("--state-dir"), 1)
        self.assertEqual(launch["supervisor_pid"], DummyProcess.pid)

    def test_start_rejects_invalid_or_non_allowlisted_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "OWNER/REPOSITORY",
            ):
                github_actions.start_monitor(
                    root,
                    repository="not-a-repository",
                    run_id=1,
                )
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "not allowlisted",
            ):
                github_actions.start_monitor(
                    root,
                    repository="Other/Project",
                    run_id=1,
                )
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "hostname is not allowlisted",
            ):
                github_actions.start_monitor(
                    root,
                    repository="Example/Project",
                    run_id=1,
                    hostname="github.example.test",
                )
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "positive decimal",
            ):
                github_actions.start_monitor(
                    root,
                    repository="Example/Project",
                    run_id="01",
                )

    def test_supervisor_launch_failure_is_finalized_durably(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "could not launch",
            ):
                github_actions.start_monitor(
                    root,
                    repository="Example/Project",
                    run_id=123,
                    popen_factory=mock.Mock(side_effect=OSError("spawn failed")),
                )
            report = github_actions.monitor_status(root)
            descriptor = report["monitors"][0]
            result = core.load_object(Path(descriptor["result_path"]))

        self.assertEqual(descriptor["status"], "failed")
        self.assertEqual(descriptor["failure_kind"], "supervisor_launch_failed")
        self.assertEqual(result["status"], "unknown")

    def test_run_view_uses_one_executable_argument_even_with_spaces(self) -> None:
        runner = mock.Mock(return_value=completed_view(gh_view()))
        result = github_actions.run_view(
            {
                "gh_command": "/mnt/c/Program Files/GitHub CLI/gh.exe",
                "run_id": 123,
                "hostname": "github.com",
                "repository": "Example/Project",
                "attempt": 1,
            },
            runner=runner,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            runner.call_args.args[0][0],
            "/mnt/c/Program Files/GitHub CLI/gh.exe",
        )
        self.assertIn("github.com/Example/Project", runner.call_args.args[0])
        self.assertFalse(runner.call_args.kwargs.get("shell", False))

    def test_view_identity_rejects_another_repository_or_host(self) -> None:
        data = {
            "run_id": 123,
            "attempt": 1,
            "expected_head_sha": "abcdef1",
            "hostname": "github.com",
            "repository": "Example/Project",
        }
        wrong_repository = gh_view()
        wrong_repository["url"] = "https://github.com/Other/Project/actions/runs/123"
        wrong_host = gh_view()
        wrong_host["url"] = "https://enterprise.example/Example/Project/actions/runs/123"

        self.assertEqual(
            github_actions.validate_view_identity(data, wrong_repository),
            "repository_url_mismatch",
        )
        self.assertEqual(
            github_actions.validate_view_identity(data, wrong_host),
            "repository_url_mismatch",
        )

    def test_start_rejects_non_finite_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(value=value), self.assertRaisesRegex(
                    github_actions.GitHubActionsError,
                    "finite positive number",
                ):
                    github_actions.start_monitor(
                        root,
                        repository="Example/Project",
                        run_id=123,
                        timeout_seconds=value,
                    )

    def test_view_failure_is_classified_without_claiming_ci_failed(self) -> None:
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=b"",
                stderr=b"authentication required; run gh auth login",
            )
        )
        observation = github_actions.observe_run(
            Path("/tmp/project"),
            {
                "gh_command": "gh",
                "run_id": 123,
                "hostname": "github.com",
                "repository": "Example/Project",
                "attempt": None,
            },
            state_dir=core.DEFAULT_STATE_DIR,
            view_runner=runner,
        )

        self.assertEqual(observation["monitor_status"], "unavailable")
        self.assertEqual(observation["failure_kind"], "authentication_failed")
        self.assertNotIn("ci_conclusion", observation)

    def test_default_run_view_bounds_output_while_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "fake-gh"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"sys.stdout.write('x' * {github_actions.MAX_VIEW_BYTES + 1})\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            result = github_actions.run_view(
                {
                    "gh_command": str(executable),
                    "run_id": 123,
                    "hostname": "github.com",
                    "repository": "Example/Project",
                    "attempt": None,
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "view_output_too_large")
        self.assertEqual(
            result["stdout"]["size_bytes"],
            github_actions.MAX_VIEW_BYTES + 1,
        )
        self.assertNotIn("tail", result["stdout"])

    def test_missing_gh_executable_is_monitor_failure_not_ci_failure(self) -> None:
        runner = mock.Mock(side_effect=FileNotFoundError("missing gh"))
        result = github_actions.run_view(
            {
                "gh_command": "/missing/gh",
                "run_id": 123,
                "hostname": "github.com",
                "repository": "Example/Project",
                "attempt": None,
            },
            runner=runner,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "gh_command_failed")

    def test_common_gh_failures_have_stable_classification(self) -> None:
        cases = {
            "HTTP 404: run not found": "run_not_found",
            "could not resolve host github.com": "network_failure",
            "not logged into any GitHub hosts; run gh auth login": (
                "authentication_failed"
            ),
            "unexpected gh failure": "gh_command_failed",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(github_actions.classify_cli_error(message), expected)

    def test_conclusion_mapping_keeps_monitor_and_ci_axes_separate(self) -> None:
        expectations = {
            "success": ("passed", "completed"),
            "failure": ("failed", "failed"),
            "startup_failure": ("failed", "failed"),
            "cancelled": ("cancelled", "cancelled"),
            "timed_out": ("failed", "timed_out"),
            "action_required": ("failed", "action_required"),
            "neutral": ("unknown", "ambiguous"),
            "skipped": ("unknown", "ambiguous"),
        }
        for conclusion, expected in expectations.items():
            with self.subTest(conclusion=conclusion):
                observation = {
                    "monitor_status": "completed",
                    "ci_conclusion": conclusion,
                }
                self.assertEqual(
                    github_actions.verification_status(observation),
                    expected[0],
                )
                self.assertEqual(github_actions.event_status(observation), expected[1])

    def test_nonzero_watch_is_overridden_by_terminal_final_view(self) -> None:
        runner = mock.Mock(
            side_effect=[
                completed_view(gh_view(status="in_progress", conclusion=None)),
                completed_view(gh_view(status="completed", conclusion="failure")),
            ]
        )
        with mock.patch.object(
            github_actions,
            "run_watch",
            return_value={"outcome": "exited", "exit_code": 1},
        ):
            observation = github_actions.observe_run(
                Path("/tmp/project"),
                {
                    "gh_command": "gh",
                    "run_id": 123,
                    "hostname": "github.com",
                    "repository": "Example/Project",
                    "attempt": None,
                },
                state_dir=core.DEFAULT_STATE_DIR,
                view_runner=runner,
            )

        self.assertEqual(observation["monitor_status"], "completed")
        self.assertEqual(observation["ci_conclusion"], "failure")
        self.assertEqual(github_actions.verification_status(observation), "failed")

    def test_cancelled_or_timed_out_watch_stays_distinct_from_ci_failure(self) -> None:
        for outcome in ("cancelled", "timed_out"):
            with self.subTest(outcome=outcome):
                runner = mock.Mock(
                    side_effect=[
                        completed_view(
                            gh_view(status="in_progress", conclusion=None)
                        ),
                        completed_view(
                            gh_view(status="in_progress", conclusion=None)
                        ),
                    ]
                )
                with mock.patch.object(
                    github_actions,
                    "run_watch",
                    return_value={"outcome": outcome, "exit_code": -15},
                ):
                    observation = github_actions.observe_run(
                        Path("/tmp/project"),
                        {
                            "gh_command": "gh",
                            "run_id": 123,
                            "hostname": "github.com",
                            "repository": "Example/Project",
                            "attempt": None,
                        },
                        state_dir=core.DEFAULT_STATE_DIR,
                        view_runner=runner,
                    )

                self.assertEqual(observation["monitor_status"], outcome)
                self.assertNotIn("ci_conclusion", observation)

    def test_expected_sha_mismatch_fails_closed(self) -> None:
        runner = mock.Mock(return_value=completed_view(gh_view()))
        observation = github_actions.observe_run(
            Path("/tmp/project"),
            {
                "gh_command": "gh",
                "run_id": 123,
                "hostname": "github.com",
                "repository": "Example/Project",
                "attempt": None,
                "expected_head_sha": "9999999",
            },
            state_dir=core.DEFAULT_STATE_DIR,
            view_runner=runner,
        )

        self.assertEqual(observation["monitor_status"], "ambiguous")
        self.assertEqual(observation["failure_kind"], "head_sha_mismatch")

    def test_finalize_reuses_checks_and_generic_wakeup_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = (
                core.state_root(root)
                / "monitors"
                / "github-actions"
                / "gha-123"
            )
            directory.mkdir(parents=True)
            descriptor = {
                "schema_version": 1,
                "kind": github_actions.MONITOR_KIND,
                "monitor_id": "gha-123",
                "source_kind": github_actions.SOURCE_KIND,
                "status": "running",
                "hostname": "github.com",
                "repository": "Example/Project",
                "run_id": 123,
                "attempt": 1,
                "expected_head_sha": "abcdef1",
                "wake_policy": "always",
                "monitor_dir": str(directory),
                "created_at": core.utc_now(),
            }
            observation = {
                "monitor_status": "completed",
                "ci_conclusion": "success",
                "initial_view": {"ok": True, "view": gh_view()},
                "watch": None,
                "final_view": {"ok": True, "view": gh_view()},
            }
            final = github_actions.finalize_monitor(
                root,
                descriptor,
                observation,
                state_dir=core.DEFAULT_STATE_DIR,
                started_at=core.utc_now(),
                duration_seconds=1.0,
            )
            event = core.verify_terminal_event(Path(final["event_path"]))
            signals = core.inbox(root)
            checks = verification.checks_status(root, check_id="gha-123")
            message = wakeup.build_wakeup_message(root, signals[0], event)

        self.assertEqual(event["kind"], "ORCHESTRATOR_TERMINAL")
        self.assertEqual(event["operation_id"], "gha-123")
        self.assertEqual(signals[0]["kind"], "ORCHESTRATOR_FOLLOWUP_SIGNAL")
        self.assertEqual(checks["status_counts"]["passed"], 1)
        self.assertIn("source: github_actions", message)
        self.assertNotIn("task_id:", message)

    def test_success_can_be_recorded_without_waking(self) -> None:
        observation = {
            "monitor_status": "completed",
            "ci_conclusion": "success",
        }
        self.assertFalse(github_actions.should_wake("on-failure", observation))
        self.assertFalse(github_actions.should_wake("action-required", observation))
        self.assertTrue(github_actions.should_wake("always", observation))

    def test_capture_is_bounded_and_redacts_tokens(self) -> None:
        data = b"x" * github_actions.MAX_CAPTURE_BYTES + b" ghp_" + b"a" * 30
        capture = github_actions.command_capture(data)

        self.assertEqual(capture["size_bytes"], len(data))
        self.assertEqual(len(capture["sha256"]), 64)
        self.assertIn("[REDACTED]", capture["tail"])
        self.assertNotIn("ghp_", capture["tail"])
        self.assertLessEqual(
            len(capture["tail"].encode()),
            github_actions.MAX_CAPTURE_BYTES,
        )

    def test_cancel_is_durable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = github_actions.monitor_dir_for(root, "gha-1")
            directory.mkdir(parents=True)
            core.atomic_json(
                directory / "monitor.json",
                {
                    "schema_version": 1,
                    "kind": github_actions.MONITOR_KIND,
                    "monitor_id": "gha-1",
                    "status": "running",
                },
            )
            first = github_actions.cancel_monitor(
                root,
                monitor_id="gha-1",
                reason="operator requested",
            )
            second = github_actions.cancel_monitor(
                root,
                monitor_id="gha-1",
                reason="operator requested",
            )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])

    def test_run_watch_honors_durable_cancel_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = github_actions.monitor_dir_for(root, "gha-cancel")
            directory.mkdir(parents=True)
            core.atomic_json(
                directory / "monitor.json",
                {
                    "schema_version": 1,
                    "kind": github_actions.MONITOR_KIND,
                    "monitor_id": "gha-cancel",
                    "status": "running",
                },
            )
            core.atomic_json(directory / "cancel-request.json", {"reason": "stop"})
            process = RunningProcess()

            def terminate(target: RunningProcess) -> None:
                target.returncode = -15

            with mock.patch.object(
                github_actions,
                "_terminate_process",
                side_effect=terminate,
            ):
                result = github_actions.run_watch(
                    root,
                    {
                        "gh_command": "gh",
                        "run_id": 123,
                        "hostname": "github.com",
                        "repository": "Example/Project",
                        "monitor_dir": str(directory),
                        "timeout_seconds": None,
                    },
                    state_dir=core.DEFAULT_STATE_DIR,
                    popen_factory=mock.Mock(return_value=process),
                )

        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(result["exit_code"], -15)

    def test_retry_records_lineage_and_uses_current_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, gh_command="new-gh")
            directory = github_actions.monitor_dir_for(root, "gha-old")
            directory.mkdir(parents=True)
            core.atomic_json(
                directory / "monitor.json",
                {
                    "schema_version": 1,
                    "kind": github_actions.MONITOR_KIND,
                    "monitor_id": "gha-old",
                    "source_kind": github_actions.SOURCE_KIND,
                    "status": "unavailable",
                    "hostname": "github.com",
                    "repository": "Example/Project",
                    "run_id": 123,
                    "attempt": 1,
                    "expected_head_sha": "abcdef1",
                    "gh_command": "old-gh",
                    "wake_policy": "always",
                    "timeout_seconds": None,
                },
            )
            retry = github_actions.retry_monitor(
                root,
                monitor_id="gha-old",
                reason="authentication repaired",
                popen_factory=mock.Mock(return_value=DummyProcess()),
            )

        self.assertEqual(retry["monitor_id"], "gha-old-r1")
        self.assertEqual(retry["retry_of"], "gha-old")
        self.assertEqual(retry["retry_reason"], "authentication repaired")
        self.assertEqual(retry["gh_command"], "new-gh")

    def test_status_exposes_dead_supervisor_as_crashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = github_actions.monitor_dir_for(root, "gha-dead")
            directory.mkdir(parents=True)
            core.atomic_json(
                directory / "monitor.json",
                {
                    "schema_version": 1,
                    "kind": github_actions.MONITOR_KIND,
                    "monitor_id": "gha-dead",
                    "status": "running",
                    "supervisor_pid": 999999,
                },
            )
            report = github_actions.monitor_status(root)

        self.assertEqual(report["status_counts"], {"crashed": 1})
        self.assertEqual(report["monitors"][0]["failure_kind"], "supervisor_not_alive")

    def test_status_contains_unreadable_descriptor_without_failing_all(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            invalid = github_actions.monitor_dir_for(root, "gha-invalid")
            invalid.mkdir(parents=True)
            (invalid / "monitor.json").write_text("{not-json", encoding="utf-8")
            report = github_actions.monitor_status(root)
            reaped = github_actions.reap_monitors(root)

        self.assertEqual(report["status_counts"], {"invalid": 1})
        self.assertEqual(
            report["monitors"][0]["failure_kind"],
            "descriptor_unreadable",
        )
        self.assertEqual(reaped["reaped_count"], 0)
        self.assertEqual(reaped["outcomes"][0]["status"], "invalid")

    def test_supervise_with_real_fake_gh_finalizes_terminal_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            executable = root / "fake-gh"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                f"print(json.dumps({gh_view()!r}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            directory = github_actions.monitor_dir_for(root, "gha-supervise")
            directory.mkdir(parents=True)
            descriptor = {
                "schema_version": 1,
                "kind": github_actions.MONITOR_KIND,
                "monitor_id": "gha-supervise",
                "source_kind": github_actions.SOURCE_KIND,
                "status": "starting",
                "hostname": "github.com",
                "repository": "Example/Project",
                "run_id": 123,
                "attempt": 1,
                "expected_head_sha": "abcdef1",
                "gh_command": str(executable),
                "wake_policy": "always",
                "timeout_seconds": None,
                "monitor_dir": str(directory),
                "supervisor_log": str(directory / "supervisor.log"),
                "created_at": core.utc_now(),
            }
            core.atomic_json(directory / "monitor.json", descriptor)
            final = github_actions.supervise_monitor(
                root,
                monitor_id="gha-supervise",
            )

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["ci_conclusion"], "success")
        self.assertTrue(final["signal_emitted"])

    def test_second_supervisor_cannot_take_over_a_live_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = github_actions.monitor_dir_for(root, "gha-owned")
            directory.mkdir(parents=True)
            core.atomic_json(
                directory / "monitor.json",
                {
                    "schema_version": 1,
                    "kind": github_actions.MONITOR_KIND,
                    "monitor_id": "gha-owned",
                    "status": "running",
                    "supervisor_pid": os.getpid(),
                    "supervisor_identity": worker_lease.process_identity(os.getpid()),
                },
            )
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "already owned",
            ):
                github_actions.supervise_monitor(root, monitor_id="gha-owned")

    def test_reap_finalizes_dead_supervisor_once_and_enables_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, gh_command="current-gh")
            directory = github_actions.monitor_dir_for(root, "gha-crashed")
            directory.mkdir(parents=True)
            core.atomic_json(
                directory / "monitor.json",
                {
                    "schema_version": 1,
                    "kind": github_actions.MONITOR_KIND,
                    "monitor_id": "gha-crashed",
                    "source_kind": github_actions.SOURCE_KIND,
                    "status": "running",
                    "hostname": "github.com",
                    "repository": "Example/Project",
                    "run_id": 123,
                    "attempt": 1,
                    "expected_head_sha": "abcdef1",
                    "gh_command": "old-gh",
                    "wake_policy": "always",
                    "timeout_seconds": None,
                    "monitor_dir": str(directory),
                    "supervisor_log": str(directory / "supervisor.log"),
                    "supervisor_identity": {
                        "source": worker_lease.IDENTITY_SOURCE,
                        "pid": 999999,
                        "start_ticks": 1,
                    },
                    "created_at": core.utc_now(),
                    "started_at": core.utc_now(),
                },
            )
            first = github_actions.reap_monitors(root)
            second = github_actions.reap_monitors(root)
            retry = github_actions.retry_monitor(
                root,
                monitor_id="gha-crashed",
                reason="supervisor failure reviewed",
                popen_factory=mock.Mock(return_value=DummyProcess()),
            )

        self.assertEqual(first["reaped_count"], 1)
        self.assertEqual(second["reaped_count"], 0)
        self.assertEqual(retry["retry_of"], "gha-crashed")
        self.assertEqual(retry["gh_command"], "current-gh")

    def test_operator_reasons_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(
                github_actions.GitHubActionsError,
                "at most",
            ):
                github_actions.cancel_monitor(
                    root,
                    monitor_id="gha-1",
                    reason="x" * (github_actions.MAX_REASON_LENGTH + 1),
                )

    def test_generic_signal_is_processed_by_existing_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text("{}", encoding="utf-8")
            evidence.write_text("{}", encoding="utf-8")
            emitted = core.write_followup_event(
                root,
                operation_id="gha-1",
                source_kind="github_actions",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-1",
            )
            scan = watcher.scan_once(
                [root],
                action="record",
            )

        self.assertEqual(emitted["event"]["operation_id"], "gha-1")
        self.assertEqual(scan["new_count"], 1)

    def test_generic_signal_can_use_codex_live_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text("{}", encoding="utf-8")
            evidence.write_text("{}", encoding="utf-8")
            core.write_followup_event(
                root,
                operation_id="gha-1",
                source_kind="github_actions",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-queue-ci",
            )
            signal = core.inbox(root)[0]
            runner = mock.Mock(
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="Queued message message-ci for thread thread-1.\n",
                    stderr="",
                )
            )
            receipt = codex_app.queue_current_thread(
                root,
                signal,
                target_thread_id="thread-1",
                runner=runner,
                activator=lambda _thread_id: {"activation": "requested"},
            )

        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(receipt["operation_id"], "gha-1")
        self.assertNotIn("task_id", receipt)
        self.assertIn("operation_id: gha-1", runner.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
