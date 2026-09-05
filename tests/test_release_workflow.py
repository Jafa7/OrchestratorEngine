from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_preserves_publication_guards(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

        for marker in (
            'tags: ["v*.*.*"]',
            "actions: read",
            "contents: write",
            "persist-credentials: false",
            "git cat-file -t",
            "SOURCE_DATE_EPOCH=",
            "tools/verify_release_ci.py",
            "tools/prepare_release_bundle.py",
            "--verify-tag --draft",
            "--verify-assets",
            "--clobber",
            "draft-state.json",
            "--draft=false --latest",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, workflow)


if __name__ == "__main__":
    unittest.main()
