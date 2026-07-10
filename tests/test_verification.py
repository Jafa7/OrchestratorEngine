from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import core, verification


def write_check(root: Path, check_id: str, result: dict) -> Path:
    check_dir = verification.checks_root(root) / check_id
    check_dir.mkdir(parents=True, exist_ok=True)
    core.atomic_json(check_dir / "verification-result.json", result)
    return check_dir


def base_result(check_id: str, *, status: str = "passed") -> dict:
    return {
        "schema_version": 1,
        "kind": verification.VERIFICATION_RESULT_KIND,
        "check_id": check_id,
        "suite": "fast",
        "status": status,
        "exit_code": 0 if status == "passed" else 1,
        "started_at": "2026-07-09T00:00:00.000+00:00",
        "finished_at": "2026-07-09T00:00:01.000+00:00",
        "duration_seconds": 1.0,
        "commands": [
            {
                "label": "unit",
                "required": True,
                "status": status,
                "exit_code": 0 if status == "passed" else 1,
                "duration_seconds": 1.0,
                "command": "python -m unittest",
                "log_path": f".orchestrator/checks/{check_id}/unit.log",
                "output_line_count": 10,
            }
        ],
        "result_path": f".orchestrator/checks/{check_id}/verification-result.json",
        "summary_path": f".orchestrator/checks/{check_id}/summary.txt",
        "log_path": f".orchestrator/checks/{check_id}/full.log",
    }


class VerificationStatusTests(unittest.TestCase):
    def test_passed_check_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            check_dir = write_check(root, "CHECK-OK", base_result("CHECK-OK"))
            (check_dir / "summary.txt").write_text("passed\n", encoding="utf-8")
            (check_dir / "full.log").write_text("ok\n", encoding="utf-8")
            (check_dir / "unit.log").write_text("ok\n", encoding="utf-8")
            report = verification.checks_status(root)
        self.assertEqual(report["kind"], verification.CHECKS_STATUS_KIND)
        self.assertEqual(report["diagnostic_count"], 0)
        self.assertEqual(report["status_counts"]["passed"], 1)
        self.assertEqual(report["checks"]["CHECK-OK"]["failed_command_count"], 0)

    def test_large_verification_logs_are_info_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            check_dir = write_check(root, "CHECK-LOUD", base_result("CHECK-LOUD"))
            (check_dir / "summary.txt").write_text("passed\n", encoding="utf-8")
            (check_dir / "full.log").write_text("x" * 64, encoding="utf-8")
            (check_dir / "unit.log").write_text("y" * 32, encoding="utf-8")
            report = verification.checks_status(root, large_log_bytes=16)

        check = report["checks"]["CHECK-LOUD"]
        self.assertEqual(report["worst_severity"], "info")
        self.assertEqual(check["log_sizes"]["full_log"], 64)
        self.assertEqual(check["commands"][0]["log_size"], 32)
        self.assertEqual(check["diagnostics"][0]["code"], "verification_large_log")
        self.assertEqual(check["diagnostics"][0]["severity"], "info")

    def test_failed_check_reports_warning_and_failed_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            check_dir = write_check(
                root,
                "CHECK-FAIL",
                base_result("CHECK-FAIL", status="failed"),
            )
            (check_dir / "summary.txt").write_text("failed\n", encoding="utf-8")
            (check_dir / "full.log").write_text("fail\n", encoding="utf-8")
            (check_dir / "unit.log").write_text("fail\n", encoding="utf-8")
            report = verification.checks_status(root)
        check = report["checks"]["CHECK-FAIL"]
        self.assertEqual(report["worst_severity"], "warning")
        self.assertEqual(check["failed_command_count"], 1)
        self.assertEqual(check["failed_commands"][0]["label"], "unit")
        self.assertEqual(
            check["diagnostics"][0]["code"],
            "verification_unsuccessful",
        )

    def test_missing_summary_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            check_dir = write_check(root, "CHECK-BROKEN", base_result("CHECK-BROKEN"))
            (check_dir / "full.log").write_text("ok\n", encoding="utf-8")
            (check_dir / "unit.log").write_text("ok\n", encoding="utf-8")
            report = verification.checks_status(root)
        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(
            report["checks"]["CHECK-BROKEN"]["diagnostics"][0]["code"],
            "verification_missing_summary",
        )

    def test_check_directory_without_result_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (verification.checks_root(root) / "CHECK-MISSING").mkdir(parents=True)
            report = verification.checks_status(root)
        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(report["checks"]["CHECK-MISSING"]["status"], "missing")
        self.assertEqual(
            report["checks"]["CHECK-MISSING"]["diagnostics"][0]["code"],
            "verification_result_unreadable",
        )

    def test_check_id_mismatch_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            check_dir = write_check(root, "CHECK-DIR", base_result("CHECK-OTHER"))
            (check_dir / "summary.txt").write_text("passed\n", encoding="utf-8")
            (check_dir / "full.log").write_text("ok\n", encoding="utf-8")
            report = verification.checks_status(root)
        self.assertEqual(report["worst_severity"], "error")
        self.assertEqual(
            report["checks"]["CHECK-DIR"]["diagnostics"][0]["code"],
            "verification_check_id_mismatch",
        )

    def test_paths_outside_project_fall_back_to_check_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = base_result("CHECK-PATH")
            result["summary_path"] = "/tmp/outside-summary.txt"
            result["log_path"] = "/tmp/outside-full.log"
            check_dir = write_check(root, "CHECK-PATH", result)
            (check_dir / "summary.txt").write_text("passed\n", encoding="utf-8")
            (check_dir / "full.log").write_text("ok\n", encoding="utf-8")
            (check_dir / "unit.log").write_text("ok\n", encoding="utf-8")
            report = verification.checks_status(root)
        check = report["checks"]["CHECK-PATH"]
        self.assertEqual(check["diagnostic_count"], 0)
        self.assertEqual(check["summary_path"], str(check_dir / "summary.txt"))
        self.assertEqual(check["log_path"], str(check_dir / "full.log"))

    def test_filters_by_check_id_status_and_severity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for check_id, status in (("CHECK-OK", "passed"), ("CHECK-FAIL", "failed")):
                check_dir = write_check(
                    root,
                    check_id,
                    base_result(check_id, status=status),
                )
                (check_dir / "summary.txt").write_text(status + "\n", encoding="utf-8")
                (check_dir / "full.log").write_text(status + "\n", encoding="utf-8")
                (check_dir / "unit.log").write_text(status + "\n", encoding="utf-8")
            report = verification.checks_status(
                root,
                status="failed",
                minimum_severity="error",
            )
        self.assertEqual(list(report["checks"]), ["CHECK-FAIL"])
        self.assertEqual(report["diagnostic_count"], 0)
        self.assertEqual(report["checks"]["CHECK-FAIL"]["diagnostics"], [])

    def test_unknown_check_filter_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(verification.VerificationError):
                verification.checks_status(root, check_id="missing")


if __name__ == "__main__":
    unittest.main()
