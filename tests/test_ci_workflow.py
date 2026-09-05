from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class CIWorkflowTests(unittest.TestCase):
    def test_ci_runs_bounded_soak_and_pinned_upgrade_matrix(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        for marker in (
            "tools/run_reliability_soak.py",
            "--iterations 20 --mode full",
            "upgrade-compatibility:",
            'version: "0.10.0"',
            'version: "0.11.1"',
            'version: "0.12.0"',
            "BASELINE_SHA256",
            "REPOSITORY: ${{ github.repository }}",
            "for attempt in range(1, 4)",
            "urlopen(url, timeout=30)",
            "tools/verify_upgrade_path.py",
            ".upgrade-baseline/bin/orchestrator-engine",
            ".upgrade-current/bin/orchestrator-engine",
            "env -u PYTHONPATH",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, workflow)


if __name__ == "__main__":
    unittest.main()
