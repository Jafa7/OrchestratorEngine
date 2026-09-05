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
            "tools/validate_release_source.py",
            "--expected-sha \"${GITHUB_SHA}\"",
            ".release/source.json",
            "SOURCE_DATE_EPOCH=",
            "prerelease=${prerelease}",
            "tools/verify_release_ci.py",
            "tools/prepare_release_bundle.py",
            "--verify-tag --draft",
            '--prerelease="${{ steps.source.outputs.prerelease }}"',
            "--verify-assets",
            "--clobber",
            "draft-state.json",
            "--prerelease --latest=false",
            "--prerelease=false --latest",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, workflow)

        self.assertNotIn("git cat-file -t", workflow)
        self.assertNotIn("git merge-base --is-ancestor", workflow)


if __name__ == "__main__":
    unittest.main()
