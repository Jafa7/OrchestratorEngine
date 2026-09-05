from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import binding, core, github_pull_requests, verification

SHA = "a" * 40


def write_config(root: Path) -> None:
    path = core.state_root(root) / "integrations.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "[integrations.github_actions]",
                "enabled = true",
                'gh_command = "gh"',
                'allowed_repositories = ["Example/Project"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def pr_view(
    *,
    head_sha: str = SHA,
    state: str = "OPEN",
    draft: bool = False,
    review: str = "APPROVED",
    mergeable: str = "MERGEABLE",
    checks: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "number": 7,
        "state": state,
        "isDraft": draft,
        "headRefOid": head_sha,
        "reviewDecision": review,
        "mergeable": mergeable,
        "statusCheckRollup": checks or [],
        "url": "https://github.com/Example/Project/pull/7",
    }


def completed_view(value: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(value).encode(), stderr=b""
    )


def descriptor(root: Path, *, review_policy: str = "ignore") -> dict[str, object]:
    directory = github_pull_requests.monitor_dir_for(root, "ghpr-test")
    return {
        "schema_version": 1,
        "kind": github_pull_requests.MONITOR_KIND,
        "monitor_id": "ghpr-test",
        "source_kind": github_pull_requests.SOURCE_KIND,
        "status": "running",
        "hostname": "github.com",
        "repository": "Example/Project",
        "pr_number": 7,
        "expected_head_sha": SHA,
        "review_policy": review_policy,
        "interval_seconds": 0.01,
        "timeout_seconds": 10.0,
        "wake_policy": "always",
        "gh_command": "gh",
        "monitor_dir": str(directory),
        "supervisor_log": str(directory / "supervisor.log"),
        "created_at": core.utc_now(),
    }


class DummyProcess:
    pid = 4321

    def wait(self) -> int:
        return 0


class GitHubPullRequestTests(unittest.TestCase):
    def test_default_id_is_scoped_to_one_pr_revision(self) -> None:
        first = github_pull_requests.default_monitor_id(
            hostname="github.com",
            repository="Example/Project",
            pr_number=7,
            expected_head_sha="a" * 40,
            review_policy="ignore",
        )
        second = github_pull_requests.default_monitor_id(
            hostname="github.com",
            repository="Example/Project",
            pr_number=7,
            expected_head_sha="b" * 40,
            review_policy="ignore",
        )

        self.assertNotEqual(first, second)

    def test_snapshot_evaluation_covers_readiness_states(self) -> None:
        base = {"pr_number": 7, "expected_head_sha": SHA, "review_policy": "ignore"}
        cases = [
            (pr_view(), "ready"),
            (pr_view(head_sha="b" * 40), "head_changed"),
            (pr_view(state="MERGED"), "merged"),
            (pr_view(state="CLOSED"), "closed"),
            (pr_view(draft=True), "waiting"),
            (pr_view(mergeable="UNKNOWN"), "waiting"),
            (pr_view(mergeable="CONFLICTING"), "conflicting"),
            (
                pr_view(
                    checks=[
                        {
                            "__typename": "CheckRun",
                            "name": "unit",
                            "status": "IN_PROGRESS",
                            "conclusion": "",
                        }
                    ]
                ),
                "waiting",
            ),
            (
                pr_view(
                    checks=[
                        {
                            "__typename": "StatusContext",
                            "context": "lint",
                            "state": "FAILURE",
                        }
                    ]
                ),
                "failed_checks",
            ),
        ]
        for view, expected in cases:
            with self.subTest(expected=expected):
                snapshot = github_pull_requests.normalize_snapshot(view)
                status, _ = github_pull_requests.evaluate_snapshot(base, snapshot)
                self.assertEqual(status, expected)

    def test_approved_policy_requires_approval_and_reports_changes(self) -> None:
        base = {"pr_number": 7, "expected_head_sha": SHA, "review_policy": "approved"}
        pending = github_pull_requests.normalize_snapshot(pr_view(review=""))
        changes = github_pull_requests.normalize_snapshot(
            pr_view(review="CHANGES_REQUESTED")
        )
        approved = github_pull_requests.normalize_snapshot(pr_view())

        self.assertEqual(
            github_pull_requests.evaluate_snapshot(base, pending)[0], "waiting"
        )
        self.assertEqual(
            github_pull_requests.evaluate_snapshot(base, changes)[0],
            "changes_requested",
        )
        self.assertEqual(
            github_pull_requests.evaluate_snapshot(base, approved)[0], "ready"
        )

    def test_run_view_uses_argv_and_bounded_evidence(self) -> None:
        root = Path("/tmp/example")
        data = descriptor(root)
        runner = mock.Mock(return_value=completed_view(pr_view()))

        result = github_pull_requests.run_view(data, runner=runner)

        self.assertTrue(result["ok"])
        command = runner.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "pr", "view"])
        self.assertEqual(command[3], "7")
        self.assertNotIn("tail", result["stdout"])
        self.assertEqual(result["snapshot"]["head_sha"], SHA)

    def test_run_view_classifies_pr_errors_and_invalid_contract(self) -> None:
        data = descriptor(Path("/tmp/example"))
        missing = mock.Mock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=b"",
                stderr=b"Could not resolve to a PullRequest with the number of 7",
            )
        )
        malformed = mock.Mock(return_value=completed_view({"number": 7}))

        self.assertEqual(
            github_pull_requests.run_view(data, runner=missing)["failure_kind"],
            "pull_request_not_found",
        )
        self.assertEqual(
            github_pull_requests.run_view(data, runner=malformed)["failure_kind"],
            "invalid_view_contract",
        )

    def test_start_is_allowlisted_idempotent_and_snapshots_wake_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            popen = mock.Mock(return_value=DummyProcess())
            first = github_pull_requests.start_monitor(
                root,
                repository="Example/Project",
                pr_number=7,
                expected_head_sha=SHA,
                popen_factory=popen,
            )
            second = github_pull_requests.start_monitor(
                root,
                repository="Example/Project",
                pr_number=7,
                expected_head_sha=SHA,
                popen_factory=popen,
            )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["wake_target"]["target_thread_id"], "thread-1")
        self.assertEqual(popen.call_count, 1)

    def test_poll_waits_then_returns_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = descriptor(root)
            Path(str(data["monitor_dir"])).mkdir(parents=True)
            responses = iter(
                [
                    completed_view(
                        pr_view(
                            checks=[
                                {
                                    "__typename": "StatusContext",
                                    "context": "ci",
                                    "state": "PENDING",
                                }
                            ]
                        )
                    ),
                    completed_view(pr_view()),
                ]
            )
            outcome = github_pull_requests.poll_until_terminal(
                root,
                data,
                state_dir=core.DEFAULT_STATE_DIR,
                runner=lambda *args, **kwargs: next(responses),
                sleep=lambda seconds: None,
            )

        self.assertEqual(outcome["status"], "ready")
        self.assertEqual(outcome["sample_count"], 2)

    def test_poll_observes_cancel_during_a_long_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = descriptor(root)
            data["interval_seconds"] = 300.0
            directory = Path(str(data["monitor_dir"]))
            directory.mkdir(parents=True)

            def cancel_after_first_sleep(seconds: float) -> None:
                self.assertLessEqual(seconds, 1.0)
                core.atomic_json(
                    directory / "cancel-request.json",
                    {"kind": "test"},
                )

            outcome = github_pull_requests.poll_until_terminal(
                root,
                data,
                state_dir=core.DEFAULT_STATE_DIR,
                runner=lambda *args, **kwargs: completed_view(
                    pr_view(mergeable="UNKNOWN")
                ),
                sleep=cancel_after_first_sleep,
            )

        self.assertEqual(outcome["status"], "cancelled")
        self.assertEqual(outcome["sample_count"], 1)

    def test_finalize_writes_result_evidence_event_and_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = descriptor(root)
            Path(str(data["monitor_dir"])).mkdir(parents=True)
            snapshot = github_pull_requests.normalize_snapshot(pr_view())
            final = github_pull_requests.finalize_monitor(
                root,
                data,
                status="ready",
                failure_kind=None,
                snapshot=snapshot,
                command_evidence={"exit_code": 0},
                state_dir=core.DEFAULT_STATE_DIR,
                started_at=core.utc_now(),
                duration_seconds=1.0,
                sample_count=1,
            )
            result = core.load_object(Path(final["result_path"]))
            evidence = core.load_object(Path(final["evidence_path"]))

        self.assertEqual(result["kind"], verification.VERIFICATION_RESULT_KIND)
        self.assertEqual(evidence["monitor_status"], "ready")
        self.assertTrue(final["signal_emitted"])

    def test_supervisor_claims_descriptor_and_finalizes_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = {**descriptor(root), "status": "starting"}
            directory = Path(str(data["monitor_dir"]))
            directory.mkdir(parents=True)
            core.atomic_json(directory / "monitor.json", data)
            with mock.patch.object(
                github_pull_requests.worker_lease,
                "process_identity",
                return_value={"pid": 1234, "start_time_ticks": 10},
            ):
                final = github_pull_requests.supervise_monitor(
                    root,
                    monitor_id="ghpr-test",
                    runner=lambda *args, **kwargs: completed_view(pr_view()),
                    sleep=lambda seconds: None,
                )
            evidence_exists = Path(final["evidence_path"]).is_file()

        self.assertEqual(final["status"], "ready")
        self.assertEqual(final["sample_count"], 1)
        self.assertTrue(evidence_exists)

    def test_reap_recovers_terminal_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = descriptor(root)
            directory = Path(str(data["monitor_dir"]))
            directory.mkdir(parents=True)
            final = github_pull_requests.finalize_monitor(
                root,
                data,
                status="ready",
                failure_kind=None,
                snapshot=github_pull_requests.normalize_snapshot(pr_view()),
                command_evidence={"exit_code": 0},
                state_dir=core.DEFAULT_STATE_DIR,
                started_at=core.utc_now(),
                duration_seconds=1.0,
            )
            crashed = {**data, "status": "running", "supervisor_pid": 99999999}
            core.atomic_json(directory / "monitor.json", crashed)

            report = github_pull_requests.reap_monitors(root)

        self.assertEqual(report["recovered_count"], 1)
        self.assertEqual(report["outcomes"][0]["status"], "recovered")
        self.assertEqual(final["event_path"], report["outcomes"][0]["event_path"])


if __name__ == "__main__":
    unittest.main()
