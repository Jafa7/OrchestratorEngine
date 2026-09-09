from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_engine import binding, release_preflight


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def write_release_files(root: Path, version: str = "1.2.3") -> None:
    package = root / "src" / "orchestrator_engine"
    package.mkdir(parents=True)
    docs = root / "docs"
    docs.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "orchestrator-engine"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "uv.lock").write_text(
        f'[[package]]\nname = "orchestrator-engine"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        f"## [{version}] - 2026-09-09\n", encoding="utf-8"
    )
    marker = f"OrchestratorEngine.git@v{version}\n"
    (root / "README.md").write_text(marker, encoding="utf-8")
    (docs / "setup-guide.md").write_text(marker, encoding="utf-8")
    (docs / "upgrade-guide.md").write_text(
        f"The current release is `{version}`\n{marker}", encoding="utf-8"
    )


class ReleasePreflightTests(unittest.TestCase):
    def make_repository(self, temporary: str) -> Path:
        root = Path(temporary) / "project"
        remote = Path(temporary) / "remote.git"
        git(Path(temporary), "init", "--bare", str(remote))
        git(Path(temporary), "init", str(root))
        git(root, "config", "user.name", "Release Test")
        git(root, "config", "user.email", "release@example.invalid")
        git(root, "checkout", "-b", "main")
        git(root, "remote", "add", "origin", str(remote))
        write_release_files(root)
        git(root, "add", ".")
        git(root, "commit", "-m", "release candidate")
        git(root, "push", "-u", "origin", "main")
        return root

    def test_clean_synced_checkout_is_ready_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(temporary)
            with patch.object(
                release_preflight.diagnostics.watcher, "service_status"
            ) as status:
                status.return_value = {"status": "running", "pending_inbox_count": 0}
                report = release_preflight.run_preflight(
                    root, host="codex", offline=True
                )
                expected_sha = git(root, "rev-parse", "HEAD")

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["head_sha"], expected_sha)
        self.assertEqual(report["failed_check_count"], 0)
        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual(checks["remote_tag_absent"]["status"], "warning")
        self.assertEqual(checks["completion_delivery"]["status"], "pass")

    def test_stream_host_delivery_is_locally_provable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(temporary)
            binding.write_binding(root, host="claude")
            with patch.object(
                release_preflight.diagnostics.claude_stream, "stream_status"
            ) as stream:
                stream.return_value = {
                    "status": "fresh",
                    "pending_inbox_count": 0,
                }
                report = release_preflight.run_preflight(
                    root, offline=True, require_watcher=True
                )

        checks = {item["name"]: item for item in report["checks"]}
        delivery = checks["completion_delivery"]
        self.assertEqual(delivery["status"], "pass")
        self.assertEqual(delivery["host"], "claude")
        self.assertEqual(delivery["stream_status"], "fresh")
        self.assertIsNone(delivery["service_status"])

    def test_unarmed_stream_blocks_a_required_delivery_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(temporary)
            binding.write_binding(root, host="claude")
            with patch.object(
                release_preflight.diagnostics.claude_stream, "stream_status"
            ) as stream:
                stream.return_value = {
                    "status": "not_started",
                    "pending_inbox_count": 0,
                }
                required = release_preflight.run_preflight(
                    root, offline=True, require_watcher=True
                )
                optional = release_preflight.run_preflight(root, offline=True)

        required_checks = {item["name"]: item for item in required["checks"]}
        optional_checks = {item["name"]: item for item in optional["checks"]}
        self.assertEqual(required_checks["completion_delivery"]["status"], "fail")
        self.assertEqual(optional_checks["completion_delivery"]["status"], "warning")

    def test_untracked_whitespace_blocks_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(temporary)
            (root / "new.txt").write_text("bad trailing space \n", encoding="utf-8")
            report = release_preflight.run_preflight(root, offline=True)

        self.assertEqual(report["status"], "blocked")
        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual(checks["clean_worktree"]["status"], "fail")
        self.assertEqual(checks["whitespace"]["status"], "fail")
        self.assertIn("new.txt:1", checks["whitespace"]["reason"])

    def test_local_tag_blocks_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(temporary)
            git(root, "tag", "-a", "v1.2.3", "-m", "released")
            report = release_preflight.run_preflight(root, offline=True)

        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(checks["local_tag_absent"]["status"], "fail")

    def test_remote_tag_blocks_online_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(temporary)
            git(root, "tag", "-a", "v1.2.3", "-m", "released")
            git(root, "push", "origin", "v1.2.3")
            git(root, "tag", "-d", "v1.2.3")
            report = release_preflight.run_preflight(root)

        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(checks["local_tag_absent"]["status"], "pass")
        self.assertEqual(checks["remote_tag_absent"]["status"], "fail")

    def test_resolve_git_commit_returns_full_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(temporary)
            expected = git(root, "rev-parse", "HEAD")

            resolved = release_preflight.resolve_git_commit(root, "HEAD")

        self.assertEqual(resolved, expected)


if __name__ == "__main__":
    unittest.main()
