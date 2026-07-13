from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "tools" / "check_release_consistency.py"
with (REPO_ROOT / "pyproject.toml").open("rb") as project_file:
    PROJECT_VERSION = tomllib.load(project_file)["project"]["version"]


class ReleaseConsistencyTests(unittest.TestCase):
    def test_checkout_release_metadata_is_consistent(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(REPO_ROOT)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            f"Release metadata is consistent: {PROJECT_VERSION}",
            completed.stdout,
        )

    def test_mismatch_is_reported_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src" / "orchestrator_engine").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "pyproject.toml").write_text(
                '[project]\nname = "orchestrator-engine"\nversion = "1.2.3"\n',
                encoding="utf-8",
            )
            (root / "src" / "orchestrator_engine" / "__init__.py").write_text(
                '__version__ = "1.2.2"\n', encoding="utf-8"
            )
            (root / "uv.lock").write_text(
                '[[package]]\nname = "orchestrator-engine"\nversion = "1.2.3"\n',
                encoding="utf-8",
            )
            (root / "CHANGELOG.md").write_text(
                "## [1.2.3] - 2026-01-01\n", encoding="utf-8"
            )
            (root / "docs" / "setup-guide.md").write_text(
                "OrchestratorEngine.git@v1.2.3\n", encoding="utf-8"
            )
            (root / "docs" / "upgrade-guide.md").write_text(
                "The current release is `1.2.3`\n"
                "OrchestratorEngine.git@v1.2.3\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(CHECKER), "--root", str(root)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(
            "src/orchestrator_engine/__init__.py has version 1.2.2; expected 1.2.3",
            completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
