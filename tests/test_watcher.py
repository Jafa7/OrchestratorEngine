from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from orchestrator_engine import codex_app, core, watcher


class FakeThreadServer:
    status = "idle"
    starts = 0
    reads = 0
    resumes = 0

    def __init__(self, *_: object, **__: object) -> None:
        pass

    def __enter__(self) -> FakeThreadServer:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def notify(self, *_: object, **__: object) -> None:
        pass

    def request(self, method: str, params: dict[str, object], **__: object) -> dict:
        if method == "initialize":
            return {}
        if method == "thread/read":
            self.__class__.reads += 1
            return {
                "thread": {
                    "id": params["threadId"],
                    "status": {"type": self.status},
                }
            }
        if method == "thread/resume":
            self.__class__.resumes += 1
            return {"thread": {"id": params["threadId"], "status": {"type": "idle"}}}
        if method == "turn/start":
            self.__class__.starts += 1
            return {"turn": {"id": "turn-1", "status": "running"}}
        raise AssertionError(f"unexpected request: {method}")


class FakePopen:
    command: ClassVar[list[str]] = []
    kwargs: ClassVar[dict[str, object]] = {}

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.__class__.command = command
        self.__class__.kwargs = kwargs
        self.pid = 4242


def reset_fake_server(status: str = "idle") -> None:
    FakeThreadServer.status = status
    FakeThreadServer.starts = 0
    FakeThreadServer.reads = 0
    FakeThreadServer.resumes = 0


def write_event(root: Path, event_id: str = "event-1") -> None:
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
        event_id=event_id,
    )


class WatcherTests(unittest.TestCase):
    def test_record_marks_signal_seen_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = root / "watcher-state.json"
            write_event(root)
            first = watcher.scan_once([root], state_path=state, action="record")
            second = watcher.scan_once([root], state_path=state, action="record")
        self.assertEqual(first["new_count"], 1)
        self.assertEqual(second["new_count"], 0)

    def test_notify_writes_durable_notification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root)
            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="notify",
            )
            notification = Path(result["notifications"][0])
            notification_exists = notification.is_file()
        self.assertTrue(notification_exists)

    def test_current_thread_callback_starts_turn_when_idle(self) -> None:
        reset_fake_server("idle")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-current")
            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="current-thread-callback",
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
            )
            receipt = core.load_object(
                codex_app.thread_wakeup_receipt_path(root, "event-current")
            )
        self.assertEqual(result["thread_wakeups"][0]["status"], "woken")
        self.assertEqual(receipt["turn_id"], "turn-1")
        self.assertEqual(FakeThreadServer.reads, 1)
        self.assertEqual(FakeThreadServer.resumes, 1)
        self.assertEqual(FakeThreadServer.starts, 1)

    def test_current_thread_callback_defers_active_thread(self) -> None:
        reset_fake_server("active")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = root / "watcher-state.json"
            write_event(root, event_id="event-active")
            result = watcher.scan_once(
                [root],
                state_path=state,
                action="current-thread-callback",
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
            )
            watcher_state = watcher.load_state(state)
        self.assertEqual(result["thread_wakeups"][0]["status"], "deferred")
        self.assertEqual(watcher_state["seen_event_ids"], [])
        self.assertIn("event-active", watcher_state["deferred_events"])

    def test_service_start_writes_state_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            service_file = root / "service.json"
            state = watcher.start_service(
                [root],
                interval_seconds=5,
                state_path=root / "watcher-state.json",
                service_file=service_file,
                action="current-thread-callback",
                target_thread_id="thread-1",
                codex="codex",
                popen_factory=FakePopen,
            )
            stored = core.load_object(service_file)
        self.assertEqual(state["pid"], 4242)
        self.assertEqual(stored["kind"], watcher.SERVICE_KIND)
        self.assertIn("watch", FakePopen.command)
        self.assertTrue(FakePopen.kwargs["start_new_session"])

    def test_service_status_degrades_stale_or_mismatched_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            service_file = watcher.default_service_path(root)
            heartbeat_file = watcher.default_heartbeat_path(root)
            core.atomic_json(
                service_file,
                {
                    "schema_version": 1,
                    "kind": watcher.SERVICE_KIND,
                    "status": "running",
                    "pid": 4242,
                    "process_group": 4242,
                    "interval_seconds": 5,
                },
            )
            core.atomic_json(
                heartbeat_file,
                {
                    "schema_version": 1,
                    "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_HEARTBEAT",
                    "pid": 7777,
                    "checked_at": (
                        datetime.now(UTC) - timedelta(minutes=10)
                    ).isoformat(timespec="milliseconds"),
                },
            )
            status = watcher.service_status(
                [root],
                process_checker=lambda _pid: True,
            )
        self.assertEqual(status["status"], "degraded")
        self.assertFalse(status["heartbeat_healthy"])
        self.assertEqual(status["heartbeat_status"], "pid_mismatch")

    def test_service_status_counts_only_unseen_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-seen")
            write_event(root, event_id="event-new")
            state_path = watcher.default_state_path(root)
            service_file = watcher.default_service_path(root)
            core.atomic_json(
                state_path,
                {
                    "schema_version": 1,
                    "seen_event_ids": ["event-seen"],
                    "deferred_events": {},
                },
            )
            core.atomic_json(
                service_file,
                {
                    "schema_version": 1,
                    "kind": watcher.SERVICE_KIND,
                    "status": "running",
                    "pid": 4242,
                    "process_group": 4242,
                    "interval_seconds": 5,
                    "state_path": str(state_path),
                },
            )
            status = watcher.service_status(
                [root],
                process_checker=lambda _pid: False,
            )
        self.assertEqual(status["pending_inbox_count"], 1)

    def test_service_status_reports_unhealthy_heartbeat_when_process_dead(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            service_file = watcher.default_service_path(root)
            heartbeat_file = watcher.default_heartbeat_path(root)
            core.atomic_json(
                service_file,
                {
                    "schema_version": 1,
                    "kind": watcher.SERVICE_KIND,
                    "status": "stopped",
                    "pid": 4242,
                    "process_group": 4242,
                    "interval_seconds": 5,
                },
            )
            core.atomic_json(
                heartbeat_file,
                {
                    "schema_version": 1,
                    "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_HEARTBEAT",
                    "pid": 4242,
                    "checked_at": core.utc_now(),
                },
            )
            status = watcher.service_status(
                [root],
                process_checker=lambda _pid: False,
            )
        self.assertEqual(status["status"], "stopped")
        self.assertFalse(status["alive"])
        self.assertFalse(status["heartbeat_healthy"])
        self.assertEqual(status["heartbeat_status"], "not_alive")

    def test_service_status_reports_crashed_when_stale_running_file_has_dead_pid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            service_file = watcher.default_service_path(root)
            heartbeat_file = watcher.default_heartbeat_path(root)
            core.atomic_json(
                service_file,
                {
                    "schema_version": 1,
                    "kind": watcher.SERVICE_KIND,
                    "status": "running",
                    "pid": 4242,
                    "process_group": 4242,
                    "interval_seconds": 5,
                },
            )
            core.atomic_json(
                heartbeat_file,
                {
                    "schema_version": 1,
                    "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_HEARTBEAT",
                    "pid": 4242,
                    "checked_at": core.utc_now(),
                },
            )
            status = watcher.service_status(
                [root],
                process_checker=lambda _pid: False,
            )
        self.assertEqual(status["status"], "crashed")
        self.assertFalse(status["alive"])
        self.assertFalse(status["heartbeat_healthy"])
        self.assertEqual(status["heartbeat_status"], "not_alive")

    def test_heartbeat_age_never_goes_negative(self) -> None:
        age = watcher.heartbeat_age_seconds(
            {
                "checked_at": (
                    datetime.now(UTC) + timedelta(seconds=5)
                ).isoformat(timespec="milliseconds")
            }
        )
        self.assertEqual(age, 0.0)


if __name__ == "__main__":
    unittest.main()
