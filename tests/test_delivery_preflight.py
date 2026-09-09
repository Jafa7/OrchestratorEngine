from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from orchestrator_engine import (
    binding,
    core,
    delivery_preflight,
    watcher,
)


class DeliveryPreflightTests(unittest.TestCase):
    def test_no_binding_is_known_not_ready_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = delivery_preflight.run(
                root,
                operation_kind="worker",
                operation_id="TASK-1",
                wake_policy="always",
                wake_target=None,
                mode="warn",
            )

            self.assertEqual(report["status"], "not_ready")
            self.assertEqual(report["reason_code"], "no_binding")
            self.assertTrue(Path(report["artifact_path"]).is_file())
            stored = core.load_object(Path(report["artifact_path"]))

        self.assertEqual(stored["kind"], delivery_preflight.KIND)
        self.assertTrue(stored["point_in_time"])

    def test_require_ready_rejects_not_ready_after_writing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = delivery_preflight.run(
                root,
                operation_kind="local_check",
                operation_id="CHECK-1",
                wake_policy="always",
                wake_target=None,
                mode="require-ready",
            )

            with self.assertRaisesRegex(
                delivery_preflight.DeliveryPreflightError, "no_binding"
            ):
                delivery_preflight.enforce(report)

        self.assertIn("artifact_path", report)

    def test_off_and_wake_disabled_are_explicit_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            off = delivery_preflight.run(
                root,
                operation_kind="worker",
                operation_id="TASK-OFF",
                wake_policy="always",
                wake_target=None,
                mode="off",
            )
            disabled = delivery_preflight.run(
                root,
                operation_kind="worker",
                operation_id="TASK-NEVER",
                wake_policy="never",
                wake_target=None,
                mode="require-ready",
            )

            self.assertFalse(delivery_preflight.artifact_root(root).exists())

        self.assertEqual(off["reason_code"], "mode_off")
        self.assertEqual(disabled["reason_code"], "wake_disabled")

    def test_fresh_claude_stream_is_ready_but_only_point_in_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bound = binding.write_binding(root, host="claude")
            target = binding.wake_target_from_binding(bound)
            core.atomic_json(
                watcher.default_stream_state_path(root, host="claude"),
                {
                    "schema_version": core.SCHEMA_VERSION,
                    "seen_event_ids": [],
                    "updated_at": core.utc_now(),
                },
            )

            report = delivery_preflight.run(
                root,
                operation_kind="worker",
                operation_id="TASK-CLAUDE",
                wake_policy="always",
                wake_target=target,
                mode="require-ready",
            )
            delivery_preflight.enforce(report)

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["reason_code"], "stream_fresh")
        self.assertEqual(report["channel_lifecycle"], "session_bound")
        self.assertTrue(report["point_in_time"])
        self.assertTrue(report["binding_match"])

    def test_non_callback_service_cannot_report_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bound = binding.write_binding(
                root,
                host="codex",
                target_thread_id="synthetic-thread",
            )
            target = binding.wake_target_from_binding(bound)
            with mock.patch(
                "orchestrator_engine.watcher.service_status",
                return_value={
                    "status": "running",
                    "heartbeat_healthy": True,
                    "heartbeat_status": "fresh",
                    "heartbeat_age_seconds": 1.0,
                    "process_identity_status": "alive",
                    "action": "notify",
                    "host_filter": None,
                    "warnings": [],
                },
            ):
                report = delivery_preflight.run(
                    root,
                    operation_kind="worker",
                    operation_id="TASK-NOTIFY",
                    wake_policy="always",
                    wake_target=target,
                    mode="require-ready",
                )

        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["reason_code"], "service_action_not_callback")

    def test_probe_failure_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bound = binding.write_binding(root, host="claude")
            target = binding.wake_target_from_binding(bound)
            with mock.patch(
                "orchestrator_engine.claude_stream.stream_status",
                side_effect=OSError("probe unavailable"),
            ):
                report = delivery_preflight.run(
                    root,
                    operation_kind="worker",
                    operation_id="TASK-UNKNOWN",
                    wake_policy="always",
                    wake_target=target,
                    mode="warn",
                )

        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["reason_code"], "probe_error")

    def test_probe_failure_detail_is_bounded_for_schema_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bound = binding.write_binding(root, host="claude")
            target = binding.wake_target_from_binding(bound)
            with mock.patch(
                "orchestrator_engine.claude_stream.stream_status",
                side_effect=OSError("x" * 5000),
            ):
                report = delivery_preflight.run(
                    root,
                    operation_kind="worker",
                    operation_id="TASK-BOUNDED",
                    wake_policy="always",
                    wake_target=target,
                    mode="warn",
                )

        self.assertEqual(len(report["detail"]), 1000)

    def test_operation_id_is_hashed_and_cannot_escape_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            hostile = "../outside\\still-inside/тест"
            report = delivery_preflight.run(
                root,
                operation_kind="worker",
                operation_id=hostile,
                wake_policy="always",
                wake_target=None,
                mode="warn",
            )
            artifact = Path(report["artifact_path"]).resolve()
            artifact.relative_to(delivery_preflight.artifact_root(root).resolve())

        self.assertNotIn(hostile, str(artifact))

    @unittest.skipIf(os.name == "nt", "symlink creation is not portable on Windows")
    def test_operation_directory_symlink_cannot_redirect_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outside = root / "outside"
            outside.mkdir()
            directory = delivery_preflight.operation_dir(
                root,
                operation_kind="worker",
                operation_id="TASK-SYMLINK",
            )
            directory.parent.mkdir(parents=True)
            directory.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                delivery_preflight.DeliveryPreflightError, "symbolic links"
            ):
                delivery_preflight.run(
                    root,
                    operation_kind="worker",
                    operation_id="TASK-SYMLINK",
                    wake_policy="always",
                    wake_target=None,
                    mode="warn",
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_history_is_bounded_and_prune_keeps_newest_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = delivery_preflight.run(
                root,
                operation_kind="workstream",
                operation_id="PLAN-1:STEP-1",
                wake_policy="always",
                wake_target=None,
                mode="warn",
                now=datetime(2025, 1, 1, tzinfo=UTC),
            )
            second = delivery_preflight.run(
                root,
                operation_kind="workstream",
                operation_id="PLAN-1:STEP-1",
                wake_policy="always",
                wake_target=None,
                mode="warn",
                now=datetime(2025, 1, 2, tzinfo=UTC),
            )
            old = (datetime.now(UTC) - timedelta(days=60)).timestamp()
            os.utime(first["artifact_path"], (old, old))
            os.utime(second["artifact_path"], (old + 1, old + 1))

            removed = delivery_preflight.prune(
                root,
                cutoff_timestamp=(datetime.now(UTC) - timedelta(days=30)).timestamp(),
            )
            history = delivery_preflight.history(
                root,
                operation_kind="workstream",
                operation_id="PLAN-1:STEP-1",
                limit=1,
            )

        self.assertEqual(removed, [first["artifact_path"]])
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["total_count"], 1)
        self.assertEqual(history["entries"][0]["preflight_id"], second["preflight_id"])

    def test_configured_mode_uses_dispatch_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / core.DEFAULT_STATE_DIR / "workers.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                '[dispatch]\ncompletion_delivery_mode = "require-ready"\n',
                encoding="utf-8",
            )

            mode = delivery_preflight.configured_mode(root)

        self.assertEqual(mode, "require-ready")


if __name__ == "__main__":
    unittest.main()
