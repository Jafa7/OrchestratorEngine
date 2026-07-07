from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestrator_engine import core


class CoreTests(unittest.TestCase):
    def test_emit_writes_terminal_event_and_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text('{"status":"ok"}', encoding="utf-8")
            evidence.write_text('{"review_ready":true}', encoding="utf-8")
            output = core.write_terminal_event(
                root,
                task_id="TASK-001",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-1",
            )
            event = core.verify_terminal_event(Path(output["event_path"]))
            inbox = core.inbox(root)
        self.assertEqual(event["event_id"], "event-1")
        self.assertEqual(event["terminal_status"], "completed")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["task_id"], "TASK-001")

    def test_verify_terminal_event_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text("before", encoding="utf-8")
            evidence.write_text("evidence", encoding="utf-8")
            output = core.write_terminal_event(
                root,
                task_id="TASK-001",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-1",
            )
            result.write_text("after", encoding="utf-8")
            with self.assertRaises(core.OrchestratorError):
                core.verify_terminal_event(Path(output["event_path"]))

    def test_paradigmarium_layout_uses_existing_orchestration_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text("result", encoding="utf-8")
            evidence.write_text("evidence", encoding="utf-8")
            output = core.write_terminal_event(
                root,
                task_id="TASK-001",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-1",
                layout="paradigmarium",
            )
            inbox = core.inbox(root, layout="paradigmarium")
        self.assertEqual(
            Path(output["event_path"]).relative_to(root),
            Path(".paradigmarium/orchestration/supervisor/events/event-1.json"),
        )
        self.assertEqual(
            Path(output["signal_path"]).relative_to(root),
            Path(".paradigmarium/orchestration/orchestrator-inbox/signals/event-1.json"),
        )
        self.assertEqual(len(inbox), 1)

    def test_cleanup_prunes_old_notifications_and_compacts_service_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inbox = core.inbox_root(root)
            notification = inbox / "notifications" / "event-1.json"
            wakeup = inbox / "thread-wakeups" / "event-1.json"
            app_log = inbox / "logs" / "event-1.thread-wakeup.app-server.log"
            service_log = inbox / "logs" / "watcher-service.log"
            core.atomic_json(notification, {"event_id": "event-1"})
            core.atomic_json(wakeup, {"event_id": "event-1"})
            app_log.parent.mkdir(parents=True, exist_ok=True)
            app_log.write_text("old app log\n", encoding="utf-8")
            service_log.write_text(
                "".join(f"row {index} {'x' * 30}\n" for index in range(10)),
                encoding="utf-8",
            )
            now = datetime(2026, 7, 7, tzinfo=UTC)
            old_timestamp = (now - timedelta(days=40)).timestamp()
            for path in (notification, wakeup, app_log):
                os.utime(path, (old_timestamp, old_timestamp))
            result = core.cleanup(
                root,
                now=now,
                log_max_bytes=100,
                log_keep_bytes=60,
            )
            notification_exists = notification.exists()
            wakeup_exists = wakeup.exists()
            app_log_exists = app_log.exists()
            service_log_size = service_log.stat().st_size
        self.assertFalse(notification_exists)
        self.assertFalse(wakeup_exists)
        self.assertFalse(app_log_exists)
        self.assertLess(service_log_size, 100)
        self.assertEqual(result["removed_count"], 3)
        self.assertIn(str(service_log), result["compacted"])


if __name__ == "__main__":
    unittest.main()
