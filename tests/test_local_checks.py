from __future__ import annotations

import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import (
    binding,
    core,
    local_checks,
    platform_runtime,
    verification,
)


def write_config(
    root: Path,
    *,
    verification: str = "focused",
    expected_duration: float | None = None,
    script: str = "print('ok')",
) -> None:
    expected = (
        f"expected_duration_seconds = {expected_duration}\n"
        if expected_duration is not None
        else ""
    )
    path = root / ".orchestrator" / "checks.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""
            [suites.gate]
            verification = "{verification}"
            {expected}
            [[suites.gate.commands]]
            label = "unit"
            argv = [{sys.executable!r}, "-c", {script!r}]
            """
        ),
        encoding="utf-8",
    )


class LocalCheckTests(unittest.TestCase):
    def test_start_rejects_check_id_owned_by_another_operation_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            verification.claim_check_owner(
                root,
                operation_id="SHARED-ID",
                operation_type="github_actions",
            )

            with self.assertRaisesRegex(
                local_checks.LocalCheckError,
                "already owned by github_actions",
            ):
                local_checks.start_check(
                    root,
                    check_id="SHARED-ID",
                    suite="gate",
                    execution="foreground",
                )

        self.assertFalse(
            (verification.checks_root(root) / "SHARED-ID" / "check.json").exists()
        )

    def test_detached_run_fails_before_check_artifacts_when_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, verification="full")
            with (
                mock.patch.object(
                    platform_runtime,
                    "detached_lifecycle_supported",
                    return_value=False,
                ),
                self.assertRaises(platform_runtime.PlatformRuntimeError),
            ):
                local_checks.start_check(
                    root,
                    check_id="CHECK-UNSUPPORTED",
                    suite="gate",
                    execution="detached",
                )

            self.assertFalse(
                local_checks.check_dir(
                    root,
                    "CHECK-UNSUPPORTED",
                    state_dir=".orchestrator",
                ).exists()
            )

    def test_plan_detaches_configured_check_over_30_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, expected_duration=30.1)
            plan = local_checks.plan_check(root, suite="gate")

        self.assertEqual(plan["recommended_execution"], "detached")
        self.assertEqual(plan["estimate_source"], "configured")

    def test_plan_detaches_unknown_full_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, verification="full")
            plan = local_checks.plan_check(root, suite="gate")

        self.assertEqual(plan["recommended_execution"], "detached")
        self.assertEqual(plan["reason"], "unknown_full_verification_duration")

    def test_foreground_pass_records_history_without_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            result = local_checks.start_check(
                root,
                check_id="CHECK-1",
                suite="gate",
                execution="foreground",
            )
            plan = local_checks.plan_check(root, suite="gate")
            verification_result = core.load_object(Path(result["result_path"]))
            evidence = core.load_object(Path(result["evidence_path"]))

        self.assertEqual(result["status"], "passed")
        self.assertEqual(core.inbox(root), [])
        self.assertEqual(plan["estimate_source"], "successful_history_median")
        self.assertEqual(plan["successful_history_samples"], 1)
        self.assertEqual(
            verification_result["commands"][0]["output_tail"], []
        )
        self.assertGreater(verification_result["commands"][0]["output_bytes"], 0)
        self.assertEqual(evidence["kind"], "ORCHESTRATOR_LOCAL_CHECK_EVIDENCE")

    def test_explicit_failure_wakeup_emits_generic_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, script="print('bad'); raise SystemExit(3)")
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            result = local_checks.start_check(
                root,
                check_id="CHECK-FAIL",
                suite="gate",
                execution="foreground",
                wake_policy="on-failure",
            )
            signals = core.inbox(root)
            verification_result = core.load_object(Path(result["result_path"]))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["source_kind"], "local_check")
        self.assertEqual(
            verification_result["commands"][0]["output_tail"], ["bad"]
        )

    def test_command_timeout_is_explicit_in_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, script="import time; time.sleep(1)")
            path = root / ".orchestrator" / "checks.toml"
            path.write_text(
                path.read_text(encoding="utf-8") + "timeout_seconds = 0.05\n",
                encoding="utf-8",
            )
            result = local_checks.start_check(
                root,
                check_id="CHECK-TIMEOUT",
                suite="gate",
                execution="foreground",
            )
            verification_result = core.load_object(Path(result["result_path"]))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(verification_result["commands"][0]["status"], "timed_out")

    def test_auto_detached_check_completes_and_keeps_dispatch_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, verification="full")
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            dispatched = local_checks.start_check(
                root,
                check_id="CHECK-DETACHED",
                suite="gate",
            )
            binding.write_binding(root, host="codex", target_thread_id="thread-2")
            for _ in range(100):
                status = local_checks.check_status(
                    root,
                    check_id="CHECK-DETACHED",
                )
                if status["checks"][0]["status"] == "passed":
                    break
                time.sleep(0.05)
            else:
                self.fail("detached check did not finish")
            signals = core.inbox(root)

        self.assertEqual(dispatched["execution"], "detached")
        self.assertEqual(status["checks"][0]["status"], "passed")
        self.assertEqual(signals[0]["wake_target"]["target_thread_id"], "thread-1")

    def test_detached_dispatch_persists_supervisor_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(
                root,
                verification="full",
                script="import time; time.sleep(0.3)",
            )
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            local_checks.start_check(
                root,
                check_id="CHECK-IDENTITY",
                suite="gate",
            )
            descriptor = core.load_object(
                local_checks.descriptor_path(
                    root, "CHECK-IDENTITY", state_dir=".orchestrator"
                )
            )
            for _ in range(100):
                current = local_checks.check_status(
                    root, check_id="CHECK-IDENTITY"
                )["checks"][0]
                if current["status"] == "passed":
                    break
                time.sleep(0.02)
            else:
                self.fail("detached identity check did not finish")

        self.assertIsInstance(descriptor.get("supervisor_pid"), int)
        self.assertIsInstance(descriptor.get("supervisor_identity"), dict)

    def test_reap_finalizes_check_when_supervisor_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            directory = local_checks.check_dir(
                root, "CHECK-DEAD", state_dir=".orchestrator"
            )
            directory.mkdir(parents=True)
            core.atomic_json(
                directory / "check.json",
                {
                    "schema_version": 1,
                    "kind": local_checks.CHECK_KIND,
                    "check_id": "CHECK-DEAD",
                    "suite": "gate",
                    "verification": "focused",
                    "fingerprint": "a" * 64,
                    "status": "running",
                    "execution": "detached",
                    "wake_policy": "never",
                    "created_at": core.utc_now(),
                    "started_at": core.utc_now(),
                    "check_dir": str(directory),
                    "plan": {},
                    "supervisor_identity": {"pid": 123},
                },
            )
            with mock.patch.object(
                local_checks.worker_lease,
                "identity_state",
                return_value={"state": "gone", "identity_verified": True},
            ):
                report = local_checks.reap_checks(root, check_id="CHECK-DEAD")
            descriptor = core.load_object(directory / "check.json")
            result_exists = Path(descriptor["result_path"]).is_file()

        self.assertEqual(report["reaped_count"], 1)
        self.assertEqual(descriptor["status"], "errored")
        self.assertTrue(result_exists)

    def test_reap_recovers_terminal_artifacts_without_replacing_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            directory = local_checks.check_dir(
                root, "CHECK-RECOVER", state_dir=".orchestrator"
            )
            directory.mkdir(parents=True)
            descriptor = {
                "schema_version": 1,
                "kind": local_checks.CHECK_KIND,
                "check_id": "CHECK-RECOVER",
                "suite": "gate",
                "verification": "focused",
                "fingerprint": "a" * 64,
                "status": "running",
                "execution": "detached",
                "wake_policy": "never",
                "created_at": core.utc_now(),
                "started_at": core.utc_now(),
                "check_dir": str(directory),
                "plan": {},
                "supervisor_identity": {"pid": 123},
            }
            core.atomic_json(directory / "check.json", descriptor)
            core.atomic_json(
                directory / "verification-result.json",
                {
                    "schema_version": 1,
                    "kind": verification.VERIFICATION_RESULT_KIND,
                    "check_id": "CHECK-RECOVER",
                    "suite": "gate",
                    "status": "passed",
                    "exit_code": 0,
                    "started_at": descriptor["started_at"],
                    "finished_at": core.utc_now(),
                    "duration_seconds": 1.0,
                    "commands": [],
                },
            )
            (directory / "summary.txt").write_text("passed\n", encoding="utf-8")
            core.atomic_json(
                directory / "evidence.json",
                {
                    "schema_version": 1,
                    "kind": "ORCHESTRATOR_LOCAL_CHECK_EVIDENCE",
                    "check_id": "CHECK-RECOVER",
                },
            )
            with mock.patch.object(
                local_checks.worker_lease,
                "identity_state",
                return_value={"state": "gone", "identity_verified": True},
            ):
                report = local_checks.reap_checks(root, check_id="CHECK-RECOVER")
            recovered = core.load_object(directory / "check.json")

        self.assertEqual(report["recovered_count"], 1)
        self.assertEqual(recovered["status"], "passed")
        self.assertIn("recovered_at", recovered)

    def test_config_rejects_command_cwd_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            path = root / ".orchestrator" / "checks.toml"
            text = path.read_text(encoding="utf-8")
            path.write_text(text + 'cwd = ".."\n', encoding="utf-8")
            with self.assertRaisesRegex(local_checks.LocalCheckError, "escapes"):
                local_checks.plan_check(root, suite="gate")

    def test_config_rejects_colliding_command_log_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            path = root / ".orchestrator" / "checks.toml"
            path.write_text(
                path.read_text(encoding="utf-8")
                + textwrap.dedent(
                    f"""

                    [[suites.gate.commands]]
                    label = "unit!"
                    argv = [{sys.executable!r}, "-c", "print('again')"]
                    """
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(local_checks.LocalCheckError, "collides"):
                local_checks.plan_check(root, suite="gate")

    def test_history_is_bounded_to_ten_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for index in range(12):
                local_checks.record_history(
                    root,
                    state_dir=".orchestrator",
                    fingerprint="a" * 64,
                    sample={
                        "check_id": f"C-{index}",
                        "status": "passed",
                        "duration_seconds": float(index),
                        "finished_at": core.utc_now(),
                    },
                )
            history = local_checks.load_history(root, state_dir=".orchestrator")

        rows = history["entries"]["a" * 64]
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["check_id"], "C-2")

    def test_legacy_checks_reader_accepts_first_class_running_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = local_checks.check_dir(
                root, "CHECK-RUNNING", state_dir=".orchestrator"
            )
            core.atomic_json(
                directory / "check.json",
                {
                    "schema_version": 1,
                    "kind": local_checks.CHECK_KIND,
                    "check_id": "CHECK-RUNNING",
                    "suite": "gate",
                    "status": "running",
                    "execution": "detached",
                },
            )
            report = verification.checks_status(root)

        self.assertEqual(report["checks"]["CHECK-RUNNING"]["status"], "running")
        self.assertEqual(report["diagnostic_count"], 0)
