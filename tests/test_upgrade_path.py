from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from tools import verify_upgrade_path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "tools" / "verify_upgrade_path.py"
CURRENT_CLI = Path(sys.executable).with_name("orchestrator-engine")


class UpgradePathTests(unittest.TestCase):
    @mock.patch.object(
        verify_upgrade_path.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["orchestrator-engine"], 30),
    )
    def test_cli_timeout_is_a_bounded_error(self, _run: object) -> None:
        with self.assertRaisesRegex(
            verify_upgrade_path.UpgradePathError,
            "timed out",
        ):
            verify_upgrade_path.run_cli(
                Path("orchestrator-engine"), Path("/project"), "adopt"
            )

    @mock.patch.object(verify_upgrade_path.subprocess, "run")
    def test_upgrade_check_can_parse_blocked_exit_status(
        self, run: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            2,
            stdout=json.dumps(
                {"kind": "ORCHESTRATOR_UPGRADE_CHECK", "status": "blocked"}
            ),
            stderr="",
        )

        report = verify_upgrade_path.run_cli(
            Path("orchestrator-engine"),
            Path("/project"),
            "upgrade",
            "check",
            allowed_returncodes=(0, 2),
        )

        self.assertEqual(report["status"], "blocked")

    def test_current_installed_cli_round_trip_is_read_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CHECKER),
                    "--baseline-cli",
                    str(CURRENT_CLI),
                    "--current-cli",
                    str(CURRENT_CLI),
                    "--fixture-root",
                    str(fixture),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            fixture_exists = fixture.exists()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["kind"], "ORCHESTRATOR_UPGRADE_PATH_REPORT")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["first_scan_count"], 1)
        self.assertEqual(report["second_scan_count"], 0)
        self.assertTrue(report["durable_artifacts_preserved"])
        self.assertEqual(report["fixture_status"], "removed")
        self.assertFalse(fixture_exists)

    def test_existing_fixture_is_rejected_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            fixture.mkdir()
            marker = fixture / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CHECKER),
                    "--baseline-cli",
                    str(CURRENT_CLI),
                    "--current-cli",
                    str(CURRENT_CLI),
                    "--fixture-root",
                    str(fixture),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            marker_content = marker.read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 1)
        self.assertIn("fixture root already exists", completed.stderr)
        self.assertEqual(marker_content, "keep\n")

    @mock.patch.object(
        verify_upgrade_path,
        "verify_upgrade_path",
        side_effect=verify_upgrade_path.UpgradePathError("synthetic failure"),
    )
    def test_failed_verification_retains_explicit_fixture(
        self, _verify: object
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            stderr = StringIO()
            with redirect_stderr(stderr):
                exit_code = verify_upgrade_path.main(
                    [
                        "--baseline-cli",
                        str(CURRENT_CLI),
                        "--current-cli",
                        str(CURRENT_CLI),
                        "--fixture-root",
                        str(fixture),
                    ]
                )
            fixture_exists = fixture.is_dir()

        self.assertEqual(exit_code, 1)
        self.assertTrue(fixture_exists)
        self.assertIn("synthetic failure", stderr.getvalue())
        self.assertIn("fixture retained", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
