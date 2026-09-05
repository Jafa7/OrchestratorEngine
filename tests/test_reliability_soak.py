from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools import run_reliability_soak

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "tools" / "run_reliability_soak.py"
CURRENT_CLI = Path(sys.executable).with_name("orchestrator-engine")


class ReliabilitySoakTests(unittest.TestCase):
    @mock.patch.object(
        run_reliability_soak.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["orchestrator-engine"], 30),
    )
    def test_outer_timeout_produces_a_bounded_failure_report(
        self, _run: object
    ) -> None:
        report = run_reliability_soak.run_soak(
            Path("orchestrator-engine"),
            iterations=3,
            mode="full",
            timeout_seconds=15,
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["iterations_completed"], 1)
        self.assertEqual(report["failure"]["iteration"], 1)
        self.assertEqual(report["failure"]["type"], "outer_timeout")
        self.assertLessEqual(len(report["failure"]["message"]), 500)

    def test_installed_portable_conformance_repeats_with_bounded_report(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--cli",
                str(CURRENT_CLI),
                "--iterations",
                "3",
                "--mode",
                "portable",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["kind"], "ORCHESTRATOR_RELIABILITY_SOAK_REPORT")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["iterations_completed"], 3)
        self.assertNotIn("steps", report)
        self.assertNotIn("stdout", report)

    def test_failed_conformance_report_is_bounded(self) -> None:
        failure_message = "x" * 1000
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        conformance = {
            "status": "failed",
            "failure": {"type": "assertion", "message": failure_message},
            "fixture": {"status": "retained", "root": "/tmp/fixture"},
        }
        with mock.patch.object(
            run_reliability_soak,
            "conformance_run",
            return_value=(completed, conformance),
        ):
            report = run_reliability_soak.run_soak(
                Path("orchestrator-engine"),
                iterations=3,
                mode="full",
                timeout_seconds=15,
            )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["iterations_completed"], 1)
        self.assertEqual(report["failure"]["exit_code"], 1)
        self.assertEqual(report["failure"]["type"], "assertion")
        self.assertEqual(len(report["failure"]["message"]), 500)
        self.assertEqual(report["failure"]["fixture"]["status"], "retained")

    def test_invalid_iteration_count_fails_before_running_conformance(self) -> None:
        for iterations in (0, run_reliability_soak.MAX_ITERATIONS + 1):
            with self.subTest(iterations=iterations):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(RUNNER),
                        "--cli",
                        str(CURRENT_CLI),
                        "--iterations",
                        str(iterations),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(completed.returncode, 1)
                self.assertIn("iterations must be between", completed.stderr)


if __name__ == "__main__":
    unittest.main()
