from __future__ import annotations

import errno
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from orchestrator_engine import core


class CoreTests(unittest.TestCase):
    def test_load_object_retries_transient_enodata(self) -> None:
        transient = OSError(errno.ENODATA, "No data available")
        with mock.patch.object(
            Path,
            "read_text",
            side_effect=[transient, '{"ok": true}'],
        ) as read_text:
            value = core.load_object(Path("state.json"))

        self.assertEqual(value, {"ok": True})
        self.assertEqual(read_text.call_count, 2)

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

    def test_verify_terminal_event_rejects_non_integer_schema_version(self) -> None:
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
            )
            event_path = Path(output["event_path"])
            event = core.load_object(event_path)
            event["schema_version"] = True
            core.atomic_json(event_path, event)

            with self.assertRaises(core.OrchestratorError):
                core.verify_terminal_event(event_path)

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
            service_log_content = service_log.read_text(encoding="utf-8")
            service_log_size = service_log.stat().st_size
        self.assertFalse(notification_exists)
        self.assertFalse(wakeup_exists)
        self.assertFalse(app_log_exists)
        self.assertLess(service_log_size, 100)
        self.assertGreater(service_log_size, 0)
        self.assertIn("row 9 ", service_log_content)
        self.assertEqual(result["removed_count"], 3)
        self.assertIn(str(service_log), result["compacted"])

    def test_compact_line_log_keeps_tail_and_drops_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "watcher-service.log"
            path.write_text(
                "".join(f"line-{index}\n" for index in range(100)),
                encoding="utf-8",
            )
            core.compact_line_log(path, keep_bytes=50)
            content = path.read_text(encoding="utf-8")
        self.assertGreater(len(content), 0)
        self.assertIn("line-99\n", content)
        self.assertNotIn("line-0\n", content)
        self.assertLessEqual(len(content.encode("utf-8")), 50 + len("line-99\n"))

    def test_survey_schema_versions_buckets_supported_and_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text('{"status":"ok"}', encoding="utf-8")
            evidence.write_text('{"review_ready":true}', encoding="utf-8")
            core.write_terminal_event(
                root,
                task_id="TASK-001",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-supported",
            )
            core.atomic_json(
                core.events_root(root) / "event-future.json",
                {
                    "schema_version": 999,
                    "kind": "WORKER_TERMINAL",
                    "event_id": "event-future",
                },
            )
            survey = core.survey_schema_versions(root)

        self.assertEqual(survey["supported_count"], 2)
        self.assertEqual(survey["unsupported_count"], 1)
        self.assertEqual(survey["unsupported"][0]["schema_version"], 999)

    def test_survey_schema_versions_reports_unreadable_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            broken = core.inbox_root(root) / "signals" / "broken.json"
            broken.parent.mkdir(parents=True)
            broken.write_text("{not-json", encoding="utf-8")
            survey = core.survey_schema_versions(root)
            still_exists = broken.is_file()

        self.assertEqual(survey["unreadable_count"], 1)
        self.assertTrue(still_exists)

    def test_survey_schema_versions_rejects_non_integer_schema_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core.atomic_json(
                core.events_root(root) / "bool-schema.json",
                {"schema_version": True, "kind": "WORKER_TERMINAL"},
            )
            core.atomic_json(
                core.events_root(root) / "float-schema.json",
                {"schema_version": 1.0, "kind": "WORKER_TERMINAL"},
            )
            survey = core.survey_schema_versions(root)

        self.assertEqual(survey["supported_count"], 0)
        self.assertEqual(survey["unsupported_count"], 2)

    def test_survey_schema_versions_includes_watcher_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = core.inbox_root(root) / "watcher-state.json"
            core.atomic_json(
                state,
                {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_STATE",
                },
            )
            survey = core.survey_schema_versions(root)

        self.assertEqual(survey["supported_count"], 1)
        self.assertEqual(survey["supported"][0]["path"], str(state))

    def test_survey_schema_versions_includes_worker_task_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = core.state_root(root) / "tasks" / "TASK-001"
            task = task_dir / "task.json"
            result = task_dir / "result.json"
            evidence = task_dir / "evidence.json"
            for path, kind in (
                (task, "WORKER_TASK"),
                (result, "WORKER_RESULT"),
                (evidence, "WORKER_EVIDENCE"),
            ):
                core.atomic_json(
                    path,
                    {
                        "schema_version": core.SCHEMA_VERSION,
                        "kind": kind,
                        "task_id": "TASK-001",
                    },
                )
            survey = core.survey_schema_versions(root)

        self.assertEqual(survey["supported_count"], 3)
        self.assertEqual(
            {item["path"] for item in survey["supported"]},
            {str(task), str(result), str(evidence)},
        )


if __name__ == "__main__":
    unittest.main()
