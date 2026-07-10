from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "examples" / "check_runner.py"


class CheckRunnerTests(unittest.TestCase):
    def run_runner(self, project: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), "--project-root", str(project), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def load_result(self, project: Path, check_id: str) -> dict:
        path = (
            project
            / ".orchestrator"
            / "checks"
            / check_id
            / "verification-result.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_single_command_pass_writes_compact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            completed = self.run_runner(
                project,
                "--check-id",
                "ok",
                "--label",
                "unit",
                "--",
                sys.executable,
                "-c",
                "print('ok')",
            )
            result = self.load_result(project, "ok")
            summary = (
                project / ".orchestrator" / "checks" / "ok" / "summary.txt"
            ).read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["kind"], "ORCHESTRATOR_VERIFICATION_RESULT")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["commands"][0]["label"], "unit")
        self.assertEqual(result["commands"][0]["status"], "passed")
        self.assertIn("Status: passed", completed.stdout)
        self.assertIn("unit [passed]", summary)

    def test_single_command_failure_keeps_failure_tail_and_full_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            completed = self.run_runner(
                project,
                "--check-id",
                "fail",
                "--label",
                "unit",
                "--",
                sys.executable,
                "-c",
                "print('before failure'); raise SystemExit(3)",
            )
            result = self.load_result(project, "fail")
            run_dir = project / ".orchestrator" / "checks" / "fail"
            summary = (run_dir / "summary.txt").read_text(encoding="utf-8")
            full_log = (run_dir / "full.log").read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["commands"][0]["exit_code"], 3)
        self.assertIn("before failure", result["commands"][0]["output_tail"])
        self.assertIn("Failure excerpts:", summary)
        self.assertIn("before failure", full_log)

    def test_failure_excerpt_lines_bounds_summary_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            completed = self.run_runner(
                project,
                "--check-id",
                "bounded-fail",
                "--label",
                "unit",
                "--tail-lines",
                "5",
                "--failure-excerpt-lines",
                "2",
                "--",
                sys.executable,
                "-c",
                (
                    "[print(f'line-{i}') for i in range(5)]; "
                    "raise SystemExit(1)"
                ),
            )
            run_dir = project / ".orchestrator" / "checks" / "bounded-fail"
            summary = (run_dir / "summary.txt").read_text(encoding="utf-8")
            full_log = (run_dir / "full.log").read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("line-0", summary)
        self.assertNotIn("line-1", summary)
        self.assertIn("line-3", summary)
        self.assertIn("line-4", summary)
        self.assertIn("line-0", full_log)

    def test_suite_config_runs_multiple_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            config = project / ".orchestrator" / "checks.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                textwrap.dedent(
                    f"""
                    [suites.fast]

                    [[suites.fast.commands]]
                    label = "one"
                    argv = [{sys.executable!r}, "-c", "print('one')"]

                    [[suites.fast.commands]]
                    label = "two"
                    argv = [{sys.executable!r}, "-c", "print('two')"]
                    """
                ),
                encoding="utf-8",
            )
            completed = self.run_runner(
                project,
                "--check-id",
                "suite",
                "--suite",
                "fast",
            )
            result = self.load_result(project, "suite")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["suite"], "fast")
        self.assertEqual([item["label"] for item in result["commands"]], ["one", "two"])
        self.assertTrue(
            all(item["status"] == "passed" for item in result["commands"])
        )


if __name__ == "__main__":
    unittest.main()
