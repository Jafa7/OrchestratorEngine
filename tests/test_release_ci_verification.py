from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "tools" / "verify_release_ci.py"
SHA = "a" * 40


def run_record(
    *,
    run_id: int,
    sha: str = SHA,
    status: str = "completed",
    conclusion: str = "success",
    repository: str = "Example/Project",
) -> dict[str, object]:
    return {
        "conclusion": conclusion,
        "databaseId": run_id,
        "event": "push",
        "headBranch": "main",
        "headSha": sha,
        "status": status,
        "url": f"https://github.com/{repository}/actions/runs/{run_id}",
        "workflowName": "CI",
    }


class ReleaseCIVerificationTests(unittest.TestCase):
    def run_checker(
        self, runs: list[dict[str, object]]
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runs.json"
            path.write_text(json.dumps(runs), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(CHECKER),
                    "--input",
                    str(path),
                    "--expected-sha",
                    SHA,
                    "--repository",
                    "Example/Project",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_exact_success_is_reported_without_accepting_stale_sha(self) -> None:
        completed = self.run_checker(
            [run_record(run_id=1, sha="b" * 40), run_record(run_id=2)]
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["run_id"], 2)
        self.assertEqual(report["head_sha"], SHA)
        self.assertEqual(report["conclusion"], "success")

    def test_latest_matching_run_must_be_successful(self) -> None:
        completed = self.run_checker(
            [
                run_record(run_id=2),
                run_record(run_id=3, status="completed", conclusion="failure"),
            ]
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("latest matching CI run", completed.stderr)

    def test_matching_run_with_wrong_repository_fails_closed(self) -> None:
        completed = self.run_checker(
            [run_record(run_id=2, repository="Other/Project")]
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("unexpected repository URL", completed.stderr)


if __name__ == "__main__":
    unittest.main()
