from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_engine import conformance, core, platform_runtime, watcher


class ConformanceTests(unittest.TestCase):
    def test_portable_run_uses_clean_fixture_and_removes_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            report = conformance.run_conformance(
                mode="portable",
                fixture_root=fixture,
            )
            fixture_exists = fixture.exists()

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["effective_mode"], "portable")
        self.assertEqual(report["fixture"]["status"], "removed")
        self.assertFalse(fixture_exists)
        self.assertEqual(report["adoption_summary"]["status"], "passed")
        self.assertEqual(report["adoption_summary"]["created_count"], 12)
        self.assertEqual(report["adoption_summary"]["worker_profile_count"], 1)
        self.assertEqual(report["adoption_summary"]["enabled_worker_count"], 0)
        self.assertEqual(report["adoption_summary"]["policy_status"], "current")
        self.assertEqual(report["artifact_summary"]["event_count"], 6)
        self.assertEqual(report["artifact_summary"]["signal_count"], 6)
        self.assertEqual(report["artifact_summary"]["notification_count"], 6)
        self.assertEqual(report["recovery_summary"]["scenario_count"], 5)
        self.assertEqual(report["recovery_summary"]["recovered_count"], 5)
        self.assertEqual(report["concurrency_summary"]["status"], "skipped")
        self.assertEqual(
            report["concurrency_summary"]["reason"],
            "full_mode_required",
        )
        self.assertEqual(report["lifecycle_recovery_summary"]["status"], "skipped")
        self.assertEqual(
            report["lifecycle_recovery_summary"]["reason"],
            "full_mode_required",
        )

    def test_portable_run_can_keep_auditable_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            report = conformance.run_conformance(
                mode="portable",
                fixture_root=fixture,
                keep_fixture=True,
            )
            state = watcher.load_state(watcher.default_state_path(fixture))
            event_id = core.terminal_event_id(
                fixture,
                task_id=conformance.SYNTHETIC_TASK_ID,
            )
            event = core.verify_terminal_event(core.event_path_for(fixture, event_id))
            signal_exists = core.signal_path_for(fixture, event_id).is_file()

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["fixture"]["status"], "retained")
        self.assertEqual(event["task_id"], conformance.SYNTHETIC_TASK_ID)
        self.assertTrue(signal_exists)
        self.assertEqual(len(state["seen_event_ids"]), 6)

    @patch(
        "orchestrator_engine.conformance.platform_runtime.detached_lifecycle_supported",
        return_value=False,
    )
    def test_auto_falls_back_to_portable_mode(self, _supported: object) -> None:
        report = conformance.run_conformance(mode="auto")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["effective_mode"], "portable")

    @unittest.skipUnless(
        platform_runtime.detached_lifecycle_supported(),
        "full conformance requires Linux detached runtime",
    )
    def test_full_run_executes_synthetic_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            report = conformance.run_conformance(
                mode="full",
                fixture_root=fixture,
                timeout_seconds=10,
            )
            fixture_exists_at_return = fixture.exists()
            time.sleep(0.1)
            fixture_reappeared = fixture.exists()

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["effective_mode"], "full")
        self.assertIn(
            "run_synthetic_worker",
            [step["name"] for step in report["steps"]],
        )
        self.assertEqual(report["artifact_summary"]["result_count"], 13)
        self.assertEqual(report["artifact_summary"]["task_descriptor_count"], 8)
        self.assertEqual(report["recovery_summary"]["recovered_count"], 5)
        self.assertEqual(report["concurrency_summary"]["status"], "passed")
        self.assertEqual(report["concurrency_summary"]["task_count"], 6)
        self.assertEqual(
            report["concurrency_summary"]["delivered_host_counts"],
            {"codex": 3, "vscode": 3},
        )
        self.assertEqual(report["lifecycle_recovery_summary"]["status"], "passed")
        self.assertEqual(report["lifecycle_recovery_summary"]["reaped_count"], 1)
        self.assertEqual(
            report["lifecycle_recovery_summary"]["failure_class"],
            "supervisor_lost",
        )
        self.assertFalse(fixture_exists_at_return)
        self.assertFalse(fixture_reappeared)

    @patch("orchestrator_engine.conformance._cancel_and_reap")
    @patch(
        "orchestrator_engine.conformance.workers.wait_for_worker_task",
        side_effect=RuntimeError("state read failed"),
    )
    @patch(
        "orchestrator_engine.conformance.workers.run_worker",
        return_value={"supervisor_pid": 123},
    )
    @patch("orchestrator_engine.conformance._write_synthetic_profile")
    def test_full_worker_settles_supervisor_after_wait_error(
        self,
        write_profile: object,
        _run_worker: object,
        _wait: object,
        cancel_and_reap: object,
    ) -> None:
        write_profile.return_value = Path("prompt.md")

        with self.assertRaisesRegex(RuntimeError, "state read failed"):
            conformance._run_full_worker(Path("/tmp/project"), 10)

        cancel_and_reap.assert_called_once_with(Path("/tmp/project"), 123)

    @patch(
        "orchestrator_engine.conformance.platform_runtime.detached_lifecycle_supported",
        return_value=False,
    )
    def test_full_mode_fails_closed_and_retains_fixture(
        self, _supported: object
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            report = conformance.run_conformance(
                mode="full",
                fixture_root=fixture,
            )
            fixture_exists = fixture.is_dir()

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["fixture"]["reason"], "failure")
        self.assertTrue(fixture_exists)
        self.assertIn("Linux detached-runtime", report["failure"]["message"])
        self.assertEqual(report["adoption_summary"]["status"], "not_run")
        self.assertEqual(report["concurrency_summary"]["status"], "not_run")
        self.assertEqual(
            report["lifecycle_recovery_summary"]["status"],
            "not_run",
        )

    def test_existing_fixture_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = conformance.run_conformance(fixture_root=Path(temporary))

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["fixture"]["status"], "not_created")
        self.assertEqual(report["fixture"]["reason"], "creation_failed")
        self.assertEqual(report["adoption_summary"]["status"], "not_run")

    @patch(
        "orchestrator_engine.conformance._create_fixture",
        side_effect=PermissionError("fixture creation denied"),
    )
    def test_fixture_creation_failure_is_a_bounded_report(
        self, _create: object
    ) -> None:
        report = conformance.run_conformance(mode="portable")

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["fixture"]["status"], "not_created")
        self.assertEqual(report["failure"]["type"], "PermissionError")
        self.assertEqual(report["steps"][0]["name"], "create_clean_fixture")
        self.assertEqual(report["steps"][0]["status"], "failed")

    def test_expanduser_failure_does_not_break_failure_report(self) -> None:
        fixture = Path("~orchestrator-conformance-user-does-not-exist/fixture")

        report = conformance.run_conformance(
            mode="portable",
            fixture_root=fixture,
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["fixture"]["status"], "not_created")
        self.assertEqual(report["fixture"]["root"], str(fixture))
        self.assertEqual(report["failure"]["type"], "RuntimeError")

    def test_portable_run_rejects_inconsistent_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            conformance.run_conformance(
                mode="portable",
                fixture_root=fixture,
                keep_fixture=True,
            )
            event_id = core.terminal_event_id(
                fixture,
                task_id=conformance.SYNTHETIC_TASK_ID,
            )
            signal_path = core.signal_path_for(fixture, event_id)
            signal = core.load_object(signal_path)
            signal["terminal_status"] = "failed"
            core.atomic_json(signal_path, signal)
            result_path = (
                fixture
                / ".orchestrator"
                / "tasks"
                / conformance.SYNTHETIC_TASK_ID
                / "result.json"
            )
            evidence_path = result_path.with_name("evidence.json")

            with self.assertRaises(conformance.ConformanceError):
                conformance._verify_artifacts(
                    fixture,
                    result_path,
                    evidence_path,
                    event_id,
                )


if __name__ == "__main__":
    unittest.main()
