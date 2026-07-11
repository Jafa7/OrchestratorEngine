from __future__ import annotations

import unittest

from orchestrator_engine import worker_diagnostics


class WorkerDiagnosticTests(unittest.TestCase):
    def test_copilot_without_autonomous_flags_warns(self) -> None:
        diagnostics = worker_diagnostics.evaluate_profile(
            name="copilot-risky",
            command=["copilot", "--prompt"],
            prompt_via="arg",
            timeout_seconds=None,
            expect_long_running=True,
        )
        self.assertEqual(
            [item["code"] for item in diagnostics],
            ["copilot_may_request_approval"],
        )
        self.assertIn("--allow-all", diagnostics[0]["suggested_action"])

    def test_copilot_with_autonomous_flags_is_clean(self) -> None:
        diagnostics = worker_diagnostics.evaluate_profile(
            name="copilot-safe",
            command=["copilot", "--prompt", "--allow-all", "--no-ask-user"],
            prompt_via="arg",
            timeout_seconds=None,
            expect_long_running=True,
        )
        self.assertEqual(diagnostics, [])

    def test_codex_exec_requires_policy_and_sandbox(self) -> None:
        diagnostics = worker_diagnostics.evaluate_profile(
            name="codex-risky",
            command=["codex", "exec", "--json"],
            prompt_via="arg",
            timeout_seconds=None,
            expect_long_running=True,
        )
        self.assertEqual(
            [item["code"] for item in diagnostics],
            ["codex_may_request_approval", "codex_missing_sandbox_strategy"],
        )

    def test_windows_executable_paths_are_detected(self) -> None:
        diagnostics = worker_diagnostics.evaluate_profile(
            name="codex-win",
            command=[r"C:\Users\me\AppData\Local\Codex\codex.exe", "exec"],
            prompt_via="arg",
            timeout_seconds=None,
            expect_long_running=True,
        )
        self.assertEqual(
            [item["code"] for item in diagnostics],
            ["codex_may_request_approval", "codex_missing_sandbox_strategy"],
        )

    def test_claude_prompt_mode_requires_permission_mode(self) -> None:
        diagnostics = worker_diagnostics.evaluate_profile(
            name="claude-risky",
            command=["claude", "-p", "--model", "sonnet"],
            prompt_via="stdin",
            timeout_seconds=None,
            expect_long_running=True,
        )
        self.assertEqual(
            [item["code"] for item in diagnostics],
            ["claude_missing_permission_mode"],
        )

    def test_official_full_access_flags_are_recognized(self) -> None:
        codex = worker_diagnostics.evaluate_profile(
            name="codex-autonomous",
            command=[
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
            ],
            prompt_via="arg",
            timeout_seconds=None,
            expect_long_running=True,
        )
        claude = worker_diagnostics.evaluate_profile(
            name="claude-autonomous",
            command=["claude", "-p", "--dangerously-skip-permissions"],
            prompt_via="stdin",
            timeout_seconds=None,
            expect_long_running=True,
        )
        self.assertEqual(codex, [])
        self.assertEqual(claude, [])

    def test_codex_sandbox_cli_flag_is_recognized(self) -> None:
        diagnostics = worker_diagnostics.evaluate_profile(
            name="codex-sandboxed",
            command=[
                "codex",
                "exec",
                "-c",
                'approval_policy="never"',
                "--sandbox",
                "workspace-write",
            ],
            prompt_via="arg",
            timeout_seconds=None,
            expect_long_running=True,
        )
        self.assertEqual(diagnostics, [])

    def test_missing_timeout_is_info_unless_long_running(self) -> None:
        quick = worker_diagnostics.evaluate_profile(
            name="quick",
            command=["python3", "-m", "pytest"],
            prompt_via="stdin",
            timeout_seconds=None,
        )
        long_running = worker_diagnostics.evaluate_profile(
            name="deep",
            command=["python3", "-m", "pytest"],
            prompt_via="stdin",
            timeout_seconds=None,
            expect_long_running=True,
        )
        self.assertEqual([item["code"] for item in quick], ["worker_timeout_absent"])
        self.assertEqual(quick[0]["severity"], "info")
        self.assertEqual(long_running, [])

    def test_filter_counts_worst_and_exit_code(self) -> None:
        diagnostics = [
            worker_diagnostics.diagnostic(
                code="note",
                severity="info",
                message="note",
                suggested_action="inspect",
            ),
            worker_diagnostics.diagnostic(
                code="warn",
                severity="warning",
                message="warn",
                suggested_action="fix",
            ),
        ]
        filtered = worker_diagnostics.filter_diagnostics(
            diagnostics,
            minimum_severity="warning",
        )
        self.assertEqual([item["code"] for item in filtered], ["warn"])
        self.assertEqual(
            worker_diagnostics.severity_counts(diagnostics),
            {"info": 1, "warning": 1, "error": 0},
        )
        self.assertEqual(worker_diagnostics.worst_severity(diagnostics), "warning")
        self.assertEqual(worker_diagnostics.exit_code_for_worst("warning"), 2)
        self.assertEqual(worker_diagnostics.exit_code_for_worst("error"), 3)
        self.assertEqual(worker_diagnostics.exit_code_for_worst("info"), 0)


if __name__ == "__main__":
    unittest.main()
