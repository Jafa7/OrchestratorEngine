from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREPARER = REPO_ROOT / "tools" / "prepare_release_bundle.py"


class ReleaseBundleTests(unittest.TestCase):
    def create_fixture(
        self, root: Path, *, version: str = "1.2.3"
    ) -> tuple[Path, Path]:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "orchestrator-engine"\nversion = "{version}"\n',
            encoding="utf-8",
        )
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n"
            f"## [{version}] - 2026-09-05\n\n### Added\n\n- Release automation.\n\n"
            "## [1.2.2] - 2026-09-01\n\n- Older change.\n",
            encoding="utf-8",
        )
        dist = root / "dist"
        dist.mkdir()
        with zipfile.ZipFile(
            dist / f"orchestrator_engine-{version}-py3-none-any.whl", "w"
        ) as archive:
            archive.writestr("orchestrator_engine/__init__.py", "# public package")
        with tarfile.open(
            dist / f"orchestrator_engine-{version}.tar.gz", "w:gz"
        ) as archive:
            content = b"# Public source"
            item = tarfile.TarInfo(f"orchestrator_engine-{version}/README.md")
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
        output = root / "release"
        return dist, output

    def run_preparer(
        self,
        root: Path,
        dist: Path,
        output: Path,
        *,
        tag: str = "v1.2.3",
        verify_assets: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(PREPARER),
            "--root",
            str(root),
            "--tag",
            tag,
            "--dist-dir",
            str(dist),
            "--output-dir",
            str(output),
        ]
        if verify_assets is not None:
            command.extend(["--verify-assets", str(verify_assets)])
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_bundle_contains_release_notes_and_distribution_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, output = self.create_fixture(root)
            completed = self.run_preparer(root, dist, output)
            report = json.loads(completed.stdout)
            notes = (output / "release-notes.md").read_text(encoding="utf-8")
            checksums = (output / "SHA256SUMS").read_text(encoding="utf-8")
            expected_digests = [
                hashlib.sha256(p.read_bytes()).hexdigest() for p in dist.iterdir()
            ]

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(report["tag"], "v1.2.3")
        self.assertFalse(report["prerelease"])
        self.assertIn("Release automation.", notes)
        self.assertNotIn("Older change.", notes)
        for expected in expected_digests:
            self.assertIn(expected, checksums)

    def test_private_or_unsafe_archive_member_blocks_publication(self):
        for member in (
            "docs/agent-forum-draft.md",
            ".agents/local/policy.md",
            ".env",
            "config.local.toml",
            "../outside",
        ):
            for extension in ("whl", "tar.gz"):
                with (
                    self.subTest(member=member, extension=extension),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    dist, output = self.create_fixture(root)
                    archive_path = next(dist.glob(f"*.{extension}"))
                    if extension == "whl":
                        with zipfile.ZipFile(archive_path, "a") as archive:
                            archive.writestr(member, "synthetic private content")
                    else:
                        with tarfile.open(archive_path, "w:gz") as archive:
                            item = tarfile.TarInfo(
                                "orchestrator_engine-1.2.3/" + member
                            )
                            archive.addfile(item, io.BytesIO(b""))
                    result = self.run_preparer(root, dist, output)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("distribution", result.stderr)
                    self.assertFalse(output.exists())

    def test_tag_must_match_project_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, output = self.create_fixture(root)
            completed = self.run_preparer(root, dist, output, tag="v1.2.4")

        self.assertEqual(completed.returncode, 1)
        self.assertIn("does not match project version", completed.stderr)

    def test_remote_assets_require_exact_names_sizes_and_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, output = self.create_fixture(root)
            prepared = self.run_preparer(root, dist, output)
            report = json.loads(prepared.stdout)
            remote = root / "remote.json"
            remote.write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                **asset,
                                "state": "uploaded",
                            }
                            for asset in report["assets"]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            verified = self.run_preparer(
                root,
                dist,
                output,
                verify_assets=remote,
            )
            payload = json.loads(remote.read_text(encoding="utf-8"))
            payload["assets"][0]["digest"] = "sha256:" + "0" * 64
            remote.write_text(json.dumps(payload), encoding="utf-8")
            mismatched = self.run_preparer(
                root,
                dist,
                output,
                verify_assets=remote,
            )

        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["remote_assets_verified"])
        self.assertEqual(mismatched.returncode, 1)
        self.assertIn("digest mismatch", mismatched.stderr)

    def test_release_candidate_bundle_is_marked_prerelease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, output = self.create_fixture(root, version="1.2.3rc1")
            completed = self.run_preparer(
                root,
                dist,
                output,
                tag="v1.2.3rc1",
            )
            report = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(report["prerelease"])


if __name__ == "__main__":
    unittest.main()
