from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "tools" / "validate_release_source.py"
TAG = "v1.2.3"
INTERNAL_REFS = (
    "refs/orchestrator/release-tag",
    "refs/orchestrator/release-main",
)


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class ReleaseRepository:
    def __init__(self, temporary: str) -> None:
        base = Path(temporary)
        self.remote = base / "remote.git"
        self.work = base / "work"
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "init", str(self.work)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.work, "config", "user.name", "Release Test")
        git(self.work, "config", "user.email", "release@example.invalid")
        git(self.work, "checkout", "-b", "main")
        git(self.work, "remote", "add", "origin", str(self.remote))
        self.commit("initial")
        git(self.work, "push", "-u", "origin", "main")

    def commit(self, content: str) -> str:
        (self.work / "payload.txt").write_text(content, encoding="utf-8")
        git(self.work, "add", "payload.txt")
        git(self.work, "commit", "-m", content)
        return git(self.work, "rev-parse", "HEAD")

    def tag_and_push(self, *, annotated: bool = True) -> str:
        args = ("tag", "-a", TAG, "-m", "release") if annotated else ("tag", TAG)
        git(self.work, *args)
        git(self.work, "push", "origin", TAG)
        return git(self.work, "rev-list", "-n", "1", TAG)

    def run_checker(self, expected_sha: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--root",
                str(self.work),
                "--tag",
                TAG,
                "--version",
                "1.2.3",
                "--expected-sha",
                expected_sha,
            ],
            check=False,
            capture_output=True,
            text=True,
        )


class ReleaseSourceTests(unittest.TestCase):
    def assert_internal_refs_removed(self, repository: ReleaseRepository) -> None:
        for reference in INTERNAL_REFS:
            with self.subTest(reference=reference):
                completed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository.work),
                        "show-ref",
                        "--verify",
                        "--quiet",
                        reference,
                    ],
                    check=False,
                )
                self.assertEqual(completed.returncode, 1)

    def test_remote_annotated_tag_survives_lightweight_local_checkout_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ReleaseRepository(temporary)
            commit_sha = repository.tag_and_push()
            git(repository.work, "update-ref", f"refs/tags/{TAG}", commit_sha)

            completed = repository.run_checker(commit_sha)
            self.assert_internal_refs_removed(repository)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["commit_sha"], commit_sha)
        self.assertTrue(report["annotated_tag_verified"])

    def test_lightweight_remote_tag_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ReleaseRepository(temporary)
            commit_sha = repository.tag_and_push(annotated=False)

            completed = repository.run_checker(commit_sha)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("must be annotated", completed.stderr)

    def test_tag_target_must_match_expected_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ReleaseRepository(temporary)
            expected_sha = git(repository.work, "rev-parse", "HEAD")
            repository.commit("tagged later")
            repository.tag_and_push()
            git(repository.work, "checkout", "--detach", expected_sha)

            completed = repository.run_checker(expected_sha)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("release tag targets", completed.stderr)

    def test_checkout_head_must_match_expected_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ReleaseRepository(temporary)
            expected_sha = repository.tag_and_push()
            repository.commit("unexpected checkout")

            completed = repository.run_checker(expected_sha)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("checkout HEAD", completed.stderr)

    def test_tagged_commit_must_be_contained_in_remote_main(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ReleaseRepository(temporary)
            git(repository.work, "checkout", "-b", "release-side")
            commit_sha = repository.commit("side release")
            repository.tag_and_push()

            completed = repository.run_checker(commit_sha)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("not contained in origin/main", completed.stderr)


if __name__ == "__main__":
    unittest.main()
