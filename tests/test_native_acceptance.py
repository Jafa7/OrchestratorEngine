from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import run_native_acceptance


class NativeAcceptanceTests(unittest.TestCase):
    def _run_report(
        self,
        *,
        system: str,
        machine: str,
        capability_platform: str,
        soak: dict[str, object] | None = None,
    ) -> dict[str, object]:
        capabilities = {
            "kind": "ORCHESTRATOR_PLATFORM_CAPABILITIES",
            "platform": capability_platform,
            "portable_core": "supported",
            "file_locking": "supported",
            "detached_lifecycle": "supported",
        }
        completed = [
            subprocess.CompletedProcess(
                [], 0, stdout="orchestrator-engine 1.6.1\n", stderr=""
            ),
            subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(capabilities), stderr=""
            ),
        ]
        soak_report = soak or {
            "schema_version": 1,
            "kind": "ORCHESTRATOR_RELIABILITY_SOAK_REPORT",
            "status": "passed",
            "mode": "full",
            "iterations_requested": 3,
            "iterations_completed": 3,
        }
        with (
            mock.patch.object(
                run_native_acceptance.subprocess, "run", side_effect=completed
            ),
            mock.patch.object(
                run_native_acceptance.run_reliability_soak,
                "run_soak",
                return_value=soak_report,
            ),
            mock.patch.object(
                run_native_acceptance.platform, "release", return_value="test-release"
            ),
            mock.patch.object(
                run_native_acceptance.platform, "machine", return_value=machine
            ),
        ):
            return run_native_acceptance.run_acceptance(
                Path("orchestrator-engine"),
                iterations=3,
                timeout_seconds=15,
                system=system,
                expected_system=system,
                expected_machine="x86_64" if system == "Windows" else machine,
            )

    def test_macos_report_is_bounded_and_separates_field_verification(self) -> None:
        report = self._run_report(
            system="Darwin", machine="arm64", capability_platform="darwin"
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["host"]["machine_family"], "arm64")
        self.assertEqual(
            report["installed_cli"]["version"], "orchestrator-engine 1.6.1"
        )
        self.assertNotIn("node", report["host"])
        self.assertEqual(
            set(report["runtime_capabilities"]["stdout"]),
            {"size_bytes", "sha256"},
        )
        self.assertEqual(
            report["field_verification"]["desktop_live_delivery"]["status"],
            "not_tested",
        )

    def test_windows_amd64_matches_normalized_x86_64_expectation(self) -> None:
        report = self._run_report(
            system="Windows", machine="AMD64", capability_platform="windows"
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["host"]["machine_family"], "x86_64")

    def test_capability_platform_mismatch_fails_report(self) -> None:
        report = self._run_report(
            system="Windows", machine="AMD64", capability_platform="darwin"
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["runtime_capabilities"]["status"], "failed")

    def test_invalid_configuration_fails_before_running_commands(self) -> None:
        with (
            mock.patch.object(run_native_acceptance.subprocess, "run") as run,
            self.assertRaisesRegex(
                run_native_acceptance.NativeAcceptanceError,
                "iterations must be between",
            ),
        ):
            run_native_acceptance.run_acceptance(
                Path("orchestrator-engine"),
                iterations=0,
                timeout_seconds=15,
                system="Darwin",
            )
        run.assert_not_called()

    def test_unsupported_host_fails_before_running_commands(self) -> None:
        with (
            mock.patch.object(run_native_acceptance.subprocess, "run") as run,
            self.assertRaisesRegex(
                run_native_acceptance.NativeAcceptanceError,
                "requires Windows or Darwin",
            ),
        ):
            run_native_acceptance.run_acceptance(
                Path("orchestrator-engine"),
                iterations=1,
                timeout_seconds=15,
                system="Linux",
            )
        run.assert_not_called()

    def test_unknown_expected_machine_fails_without_echoing_value(self) -> None:
        private_value = "/Users/private/machine"
        with (
            mock.patch.object(run_native_acceptance.subprocess, "run") as run,
            self.assertRaises(run_native_acceptance.NativeAcceptanceError) as caught,
        ):
            run_native_acceptance.run_acceptance(
                Path("orchestrator-engine"),
                iterations=1,
                timeout_seconds=15,
                system="Darwin",
                expected_machine=private_value,
            )
        run.assert_not_called()
        self.assertNotIn(private_value, str(caught.exception))

    def test_expected_system_mismatch_fails_before_running_commands(self) -> None:
        with (
            mock.patch.object(run_native_acceptance.subprocess, "run") as run,
            self.assertRaisesRegex(
                run_native_acceptance.NativeAcceptanceError,
                "expected system Windows, found Darwin",
            ),
        ):
            run_native_acceptance.run_acceptance(
                Path("orchestrator-engine"),
                iterations=1,
                timeout_seconds=15,
                system="Darwin",
                expected_system="Windows",
            )
        run.assert_not_called()

    def test_failed_soak_omits_fixture_path_and_raw_message(self) -> None:
        soak = {
            "schema_version": 1,
            "kind": "ORCHESTRATOR_RELIABILITY_SOAK_REPORT",
            "status": "failed",
            "mode": "full",
            "iterations_requested": 3,
            "iterations_completed": 2,
            "duration_seconds": 1.2,
            "conformance_duration_seconds": {},
            "failure": {
                "iteration": 2,
                "exit_code": 1,
                "type": "failed_check",
                "message": "private output at /Users/example/project",
                "fixture": "/Users/example/private-fixture",
            },
        }
        report = self._run_report(
            system="Darwin",
            machine="arm64",
            capability_platform="darwin",
            soak=soak,
        )

        serialized = json.dumps(report)
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("/Users/example", serialized)
        failure = report["reliability_soak"]["failure"]
        self.assertEqual(set(failure["message"]), {"size_bytes", "sha256"})
        self.assertTrue(failure["fixture_retained"])

    def test_capability_output_is_represented_only_by_digest(self) -> None:
        report = self._run_report(
            system="Darwin", machine="arm64", capability_platform="darwin"
        )
        expected = json.dumps(
            {
                "kind": "ORCHESTRATOR_PLATFORM_CAPABILITIES",
                "platform": "darwin",
                "portable_core": "supported",
                "file_locking": "supported",
                "detached_lifecycle": "supported",
            }
        )
        self.assertEqual(
            report["runtime_capabilities"]["stdout"]["sha256"],
            hashlib.sha256(expected.encode()).hexdigest(),
        )

    def test_cli_writes_failure_report_for_unsupported_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            with mock.patch.object(
                run_native_acceptance.platform, "system", return_value="Linux"
            ), mock.patch("sys.stdout", new=io.StringIO()):
                exit_code = run_native_acceptance.main(["--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["host"]["system"], "Linux")
        self.assertEqual(
            report["field_verification"]["desktop_live_delivery"]["status"],
            "not_tested",
        )
        self.assertTrue(report["privacy"]["hostname_omitted"])


if __name__ == "__main__":
    unittest.main()
