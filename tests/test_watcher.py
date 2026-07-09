from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from orchestrator_engine import binding, codex_app, core, vscode_chat, watcher


class FakeThreadServer:
    status = "idle"
    starts = 0
    reads = 0
    resumes = 0
    awaits = 0
    closes = 0
    turn_status = "completed"
    turn_error_message: str | None = None
    auto_declined: ClassVar[list[str]] = []
    last_codex: ClassVar[str | None] = None

    def __init__(self, codex: str = "codex", **__: object) -> None:
        type(self).last_codex = codex

    def close(self) -> None:
        self.__class__.closes += 1

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
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        raise AssertionError(f"unexpected request: {method}")

    def await_turn_outcome(
        self, _thread_id: str, turn_id: str, *, window_seconds: float
    ) -> dict | None:
        self.__class__.awaits += 1
        if self.turn_status == "running":
            return None
        turn = {"id": turn_id, "status": self.turn_status}
        if self.turn_status != "completed":
            turn["error"] = {"message": self.turn_error_message or "boom"}
        return turn


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
    FakeThreadServer.awaits = 0
    FakeThreadServer.closes = 0
    FakeThreadServer.turn_status = "completed"
    FakeThreadServer.turn_error_message = None
    FakeThreadServer.auto_declined = []
    FakeThreadServer.last_codex = None


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

    def test_current_thread_callback_defers_recently_active_thread(self) -> None:
        reset_fake_server("idle")
        original = codex_app.thread_recent_activity
        codex_app.thread_recent_activity = lambda *_args, **_kwargs: {
            "rollout_path": "/tmp/rollout.jsonl",
            "age_seconds": 2.0,
            "grace_seconds": 90.0,
        }
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                state = root / "watcher-state.json"
                write_event(root, event_id="event-recent")
                result = watcher.scan_once(
                    [root],
                    state_path=state,
                    action="current-thread-callback",
                    target_thread_id="thread-1",
                    server_factory=FakeThreadServer,
                )
                watcher_state = watcher.load_state(state)
        finally:
            codex_app.thread_recent_activity = original
        wakeup = result["thread_wakeups"][0]
        self.assertEqual(wakeup["status"], "deferred")
        self.assertEqual(wakeup["reason"], "thread_recently_active")
        self.assertEqual(FakeThreadServer.reads, 1)
        self.assertEqual(FakeThreadServer.resumes, 0)
        self.assertEqual(FakeThreadServer.starts, 0)
        self.assertEqual(watcher_state["seen_event_ids"], [])
        self.assertIn("event-recent", watcher_state["deferred_events"])

    def test_current_thread_callback_defers_when_turn_fails(self) -> None:
        reset_fake_server("idle")
        FakeThreadServer.turn_status = "failed"
        FakeThreadServer.turn_error_message = "You've hit your usage limit."
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = root / "watcher-state.json"
            write_event(root, event_id="event-rate-limited")
            result = watcher.scan_once(
                [root],
                state_path=state,
                action="current-thread-callback",
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
            )
            watcher_state = watcher.load_state(state)
        wakeup = result["thread_wakeups"][0]
        self.assertEqual(wakeup["status"], "deferred")
        self.assertIn("usage limit", wakeup["reason"])
        self.assertEqual(FakeThreadServer.awaits, 1)
        self.assertEqual(watcher_state["seen_event_ids"], [])
        self.assertIn("event-rate-limited", watcher_state["deferred_events"])
        self.assertEqual(
            watcher_state["deferred_events"]["event-rate-limited"]["status"],
            watcher.DEFER_STATUS_MANUAL_REQUIRED,
        )

    def test_callback_usage_limit_becomes_manual_required_and_stops_retrying(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_state_path(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            write_event(root, event_id="event-quota")
            calls: list[str] = []

            def fake_codex(_project, signal, **_kwargs):
                calls.append(str(signal["event_id"]))
                return {
                    "schema_version": 1,
                    "kind": "CURRENT_THREAD_WAKEUP",
                    "event_id": signal["event_id"],
                    "task_id": signal["task_id"],
                    "status": "deferred",
                    "reason": (
                        "turn failed: You've hit your usage limit. "
                        "Try again at 6:10 PM."
                    ),
                }

            first = watcher.scan_once(
                [root],
                state_path=state,
                action="callback",
                host_adapters={"codex": fake_codex},
            )
            second = watcher.scan_once(
                [root],
                state_path=state,
                action="callback",
                host_adapters={"codex": fake_codex},
            )
            watcher_state = watcher.load_state(state)
            status = watcher.service_status([root])

        self.assertEqual(first["new_count"], 1)
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(calls, ["event-quota"])
        deferred = watcher_state["deferred_events"]["event-quota"]
        self.assertEqual(deferred["status"], watcher.DEFER_STATUS_MANUAL_REQUIRED)
        self.assertEqual(deferred["reason_code"], "quota_or_usage_limit")
        self.assertNotIn("retry_after_at", deferred)
        self.assertEqual(status["deferred_event_count"], 1)
        self.assertEqual(
            status["deferred_events"][0]["status"],
            watcher.DEFER_STATUS_MANUAL_REQUIRED,
        )
        self.assertIn("acknowledge", status["deferred_events"][0]["operator_action"])

    def test_callback_generic_failure_requires_manual_after_bounded_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_state_path(root)
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            write_event(root, event_id="event-network")
            calls: list[str] = []

            def fake_codex(_project, signal, **_kwargs):
                calls.append(str(signal["event_id"]))
                return {
                    "schema_version": 1,
                    "kind": "CURRENT_THREAD_WAKEUP",
                    "event_id": signal["event_id"],
                    "task_id": signal["task_id"],
                    "status": "deferred",
                    "reason": "temporary callback transport failure",
                }

            for _attempt in range(watcher.DEFER_MAX_ATTEMPTS):
                watcher.scan_once(
                    [root],
                    state_path=state,
                    action="callback",
                    host_adapters={"codex": fake_codex},
                )
                watcher_state = watcher.load_state(state)
                deferred = watcher_state["deferred_events"]["event-network"]
                if (
                    deferred["status"]
                    != watcher.DEFER_STATUS_MANUAL_REQUIRED
                ):
                    deferred["retry_after_at"] = 0
                    core.atomic_json(state, watcher_state)
            after_limit = watcher.scan_once(
                [root],
                state_path=state,
                action="callback",
                host_adapters={"codex": fake_codex},
            )
            watcher_state = watcher.load_state(state)

        self.assertEqual(len(calls), watcher.DEFER_MAX_ATTEMPTS)
        self.assertEqual(after_limit["new_count"], 0)
        self.assertEqual(
            watcher_state["deferred_events"]["event-network"]["status"],
            watcher.DEFER_STATUS_MANUAL_REQUIRED,
        )

    def test_acknowledge_deferred_event_marks_signal_seen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_state_path(root)
            write_event(root, event_id="event-ack")
            core.atomic_json(
                state,
                {
                    "schema_version": 1,
                    "seen_event_ids": [],
                    "deferred_events": {
                        "event-ack": {
                            "status": watcher.DEFER_STATUS_MANUAL_REQUIRED,
                            "attempts": 1,
                            "reason": "turn failed: usage limit",
                            "reason_code": "quota_or_usage_limit",
                            "task_id": "TASK-001",
                        }
                    },
                    "acknowledged_events": {},
                },
            )
            ack = watcher.acknowledge_deferred_event(
                root,
                event_id="event-ack",
                state_path=state,
                reason="result read manually",
            )
            watcher_state = watcher.load_state(state)

        self.assertEqual(ack["status"], watcher.ACKNOWLEDGED_STATUS)
        self.assertEqual(ack["previous_status"], watcher.DEFER_STATUS_MANUAL_REQUIRED)
        self.assertIn("event-ack", watcher_state["seen_event_ids"])
        self.assertNotIn("event-ack", watcher_state["deferred_events"])
        self.assertIn("event-ack", watcher_state["acknowledged_events"])

    def test_list_deferred_events_reports_counts_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_state_path(root)
            write_event(root, event_id="event-list")
            signal = core.inbox(root)[0]
            core.atomic_json(
                state,
                {
                    "schema_version": 1,
                    "seen_event_ids": [],
                    "deferred_events": {
                        "event-list": {
                            "status": watcher.DEFER_STATUS_MANUAL_REQUIRED,
                            "attempts": 2,
                            "reason": "turn failed: usage limit",
                            "reason_code": "quota_or_usage_limit",
                            "task_id": "TASK-001",
                            "terminal_status": "completed",
                            "event_path": signal["event_path"],
                            "signal_path": signal["signal_path"],
                            "first_attempt_at": 1000.0,
                            "last_attempt_at": 1030.0,
                        }
                    },
                    "acknowledged_events": {},
                },
            )
            listing = watcher.list_deferred_events([root], state_path=state)

        self.assertEqual(listing["deferred_event_count"], 1)
        self.assertEqual(
            listing["deferred_status_counts"][watcher.DEFER_STATUS_MANUAL_REQUIRED],
            1,
        )
        event = listing["deferred_events"][0]
        self.assertEqual(event["event_id"], "event-list")
        self.assertEqual(event["terminal_status"], "completed")
        self.assertEqual(event["event_path"], signal["event_path"])
        self.assertEqual(event["signal_path"], signal["signal_path"])
        self.assertIsNotNone(event["first_attempt_at_iso"])

    def test_retry_deferred_event_rearms_manual_required_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_state_path(root)
            core.atomic_json(
                state,
                {
                    "schema_version": 1,
                    "seen_event_ids": [],
                    "deferred_events": {
                        "event-retry": {
                            "status": watcher.DEFER_STATUS_MANUAL_REQUIRED,
                            "attempts": 5,
                            "reason": "turn failed: usage limit",
                            "reason_code": "quota_or_usage_limit",
                            "task_id": "TASK-001",
                            "retry_after_at": 9999.0,
                        }
                    },
                    "acknowledged_events": {},
                },
            )
            retry = watcher.retry_deferred_event(
                root,
                event_id="event-retry",
                state_path=state,
                reason="quota reset",
            )
            watcher_state = watcher.load_state(state)

        self.assertEqual(retry["status"], "retry_scheduled")
        self.assertEqual(
            retry["previous_status"],
            watcher.DEFER_STATUS_MANUAL_REQUIRED,
        )
        deferred = watcher_state["deferred_events"]["event-retry"]
        self.assertEqual(deferred["status"], watcher.DEFER_STATUS_RETRYABLE)
        self.assertEqual(deferred["attempts"], 5)
        self.assertNotIn("retry_after_at", deferred)
        self.assertEqual(deferred["retry_reason"], "quota reset")

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

    def test_service_start_rejects_host_for_non_callback_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(watcher.WatcherError):
                watcher.start_service(
                    [root],
                    interval_seconds=5,
                    state_path=root / "watcher-state.json",
                    service_file=root / "service.json",
                    action="notify",
                    target_thread_id=None,
                    codex="codex",
                    host="codex",
                    popen_factory=FakePopen,
                )

    def test_callback_service_host_scope_uses_scoped_files_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            service = watcher.start_service(
                [root],
                interval_seconds=5,
                state_path=None,
                service_file=None,
                action="callback",
                target_thread_id=None,
                codex="codex",
                host="codex",
                popen_factory=FakePopen,
            )
        self.assertTrue(
            service["service_file"].endswith("watcher-codex-callback-service.json")
        )
        self.assertTrue(
            service["state_path"].endswith("watcher-codex-callback-state.json")
        )
        self.assertTrue(
            service["heartbeat_path"].endswith(
                "watcher-codex-callback-heartbeat.json"
            )
        )
        self.assertEqual(service["host_filter"], ["codex"])
        self.assertIn("--host", FakePopen.command)
        self.assertIn("codex", FakePopen.command)

    def test_watch_forwards_host_filter_to_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            calls: list[set[str] | None] = []

            def fake_scan(_projects, **kwargs):
                calls.append(kwargs.get("host_filter"))
                return {
                    "schema_version": 1,
                    "checked_at": core.utc_now(),
                    "project_roots": [str(root)],
                    "new_count": 0,
                    "new_signals": [],
                    "notifications": [],
                    "thread_wakeups": [],
                    "action_errors": [],
                    "state_path": str(root / "state.json"),
                }

            watcher.watch(
                [root],
                state_dir=core.DEFAULT_STATE_DIR,
                interval_seconds=0.01,
                state_path=root / "state.json",
                action="record",
                target_thread_id=None,
                codex="codex",
                heartbeat_file=root / "heartbeat.json",
                host_filter={"codex"},
                max_scans=1,
                scan=fake_scan,
            )
        self.assertEqual(calls, [{"codex"}])

    def test_service_status_host_scope_counts_only_matching_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text('{"status":"ok"}', encoding="utf-8")
            evidence.write_text('{"review_ready":true}', encoding="utf-8")
            core.write_terminal_event(
                root,
                task_id="TASK-CODEX",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-codex",
                wake_target={
                    "schema_version": 1,
                    "kind": binding.WAKE_TARGET_KIND,
                    "host": "codex",
                    "target_thread_id": "thread-codex",
                    "captured_at": "2026-07-08T00:00:00.000+00:00",
                },
            )
            core.write_terminal_event(
                root,
                task_id="TASK-VSCODE",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-vscode",
                wake_target={
                    "schema_version": 1,
                    "kind": binding.WAKE_TARGET_KIND,
                    "host": "vscode",
                    "captured_at": "2026-07-08T00:00:00.000+00:00",
                },
            )
            status = watcher.service_status([root], host="codex")
        self.assertTrue(
            status["service_file"].endswith("watcher-codex-callback-service.json")
        )
        self.assertEqual(status["pending_inbox_count"], 1)

    def test_service_status_host_scope_reads_state_without_service_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_callback_state_path(root, host="codex")
            core.atomic_json(
                state,
                {
                    "schema_version": 1,
                    "seen_event_ids": [],
                    "deferred_events": {
                        "event-host": {
                            "status": watcher.DEFER_STATUS_MANUAL_REQUIRED,
                            "attempts": 1,
                            "reason": "turn failed: usage limit",
                            "reason_code": "quota_or_usage_limit",
                            "task_id": "TASK-HOST",
                        }
                    },
                    "acknowledged_events": {},
                },
            )
            status = watcher.service_status([root], host="codex")

        self.assertEqual(status["status"], "not_started")
        self.assertEqual(status["state_path"], str(state))
        self.assertEqual(status["deferred_event_count"], 1)
        self.assertEqual(status["deferred_events"][0]["event_id"], "event-host")
        self.assertEqual(status["manual_required_count"], 1)

    def test_bare_service_status_warns_when_host_scoped_pending_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            write_event(root, event_id="event-host")
            core.atomic_json(
                watcher.default_state_path(root),
                {
                    "schema_version": 1,
                    "seen_event_ids": ["event-host"],
                    "deferred_events": {},
                    "acknowledged_events": {},
                },
            )
            status = watcher.service_status([root])

        self.assertEqual(status["pending_inbox_count"], 0)
        self.assertTrue(
            any("--host codex" in warning for warning in status["warnings"])
        )

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

    def test_callback_without_binding_skips_unroutable_legacy_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root)
            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="callback",
            )
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["action_errors"], [])

    def test_callback_service_can_start_without_fallback_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            service = watcher.start_service(
                [root],
                interval_seconds=5,
                state_path=root / "watcher-state.json",
                service_file=root / "service.json",
                action="callback",
                target_thread_id=None,
                codex="codex",
                popen_factory=FakePopen,
            )
        self.assertEqual(service["status"], "running")

    def test_callback_dispatches_to_bound_codex_adapter(self) -> None:
        reset_fake_server("idle")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="codex", target_thread_id="thread-9")
            write_event(root, event_id="event-callback")
            calls: list[dict] = []

            def fake_codex(project, signal, **kwargs):
                calls.append({"signal": signal, **kwargs})
                return {"status": "woken", "event_id": signal["event_id"]}

            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="callback",
                host_adapters={"codex": fake_codex},
            )
        self.assertEqual(result["thread_wakeups"][0]["status"], "woken")
        self.assertEqual(calls[0]["binding"]["target_thread_id"], "thread-9")

    def test_callback_prefers_signal_wake_target_over_current_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="codex", target_thread_id="thread-new")
            result_path = root / "result.json"
            evidence_path = root / "evidence.json"
            result_path.write_text('{"status":"ok"}', encoding="utf-8")
            evidence_path.write_text('{"ok":true}', encoding="utf-8")
            wake_target = {
                "schema_version": 1,
                "kind": binding.WAKE_TARGET_KIND,
                "host": "codex",
                "target_thread_id": "thread-origin",
                "codex_command": "/mnt/c/apps/codex.exe",
                "captured_at": "2026-07-08T00:00:00.000+00:00",
            }
            core.write_terminal_event(
                root,
                task_id="TASK-SNAPSHOT",
                terminal_status="completed",
                result_path=result_path,
                evidence_path=evidence_path,
                event_id="event-snapshot",
                wake_target=wake_target,
            )
            calls: list[dict] = []

            def fake_codex(project, signal, **kwargs):
                calls.append({"signal": signal, **kwargs})
                return {"status": "woken", "event_id": signal["event_id"]}

            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="callback",
                host_adapters={"codex": fake_codex},
            )
        self.assertEqual(result["thread_wakeups"][0]["status"], "woken")
        self.assertEqual(calls[0]["binding"]["target_thread_id"], "thread-origin")
        self.assertEqual(calls[0]["binding"]["codex_command"], "/mnt/c/apps/codex.exe")

    def test_callback_uses_binding_codex_command(self) -> None:
        reset_fake_server("idle")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(
                root,
                host="codex",
                target_thread_id="thread-9",
                codex_command="/mnt/c/apps/codex.exe",
            )
            write_event(root, event_id="event-win-codex")
            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="callback",
                server_factory=FakeThreadServer,
            )
        self.assertEqual(result["thread_wakeups"][0]["status"], "woken")
        self.assertEqual(FakeThreadServer.last_codex, "/mnt/c/apps/codex.exe")

    def test_callback_skips_stream_only_legacy_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="claude")
            write_event(root)
            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="callback",
            )
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["action_errors"], [])

    def test_callback_processes_signal_wake_target_when_binding_is_stream_host(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="claude")
            result_path = root / "result.json"
            evidence_path = root / "evidence.json"
            result_path.write_text('{"status":"ok"}', encoding="utf-8")
            evidence_path.write_text('{"ok":true}', encoding="utf-8")
            core.write_terminal_event(
                root,
                task_id="TASK-SNAPSHOT",
                terminal_status="completed",
                result_path=result_path,
                evidence_path=evidence_path,
                event_id="event-snapshot",
                wake_target={
                    "schema_version": 1,
                    "kind": binding.WAKE_TARGET_KIND,
                    "host": "codex",
                    "target_thread_id": "thread-origin",
                    "captured_at": "2026-07-08T00:00:00.000+00:00",
                },
            )
            calls: list[dict] = []

            def fake_codex(project, signal, **kwargs):
                calls.append({"signal": signal, **kwargs})
                return {"status": "woken", "event_id": signal["event_id"]}

            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="callback",
                host_adapters={"codex": fake_codex},
            )
        self.assertEqual(result["thread_wakeups"][0]["status"], "woken")
        self.assertEqual(calls[0]["binding"]["target_thread_id"], "thread-origin")

    def test_scan_skips_malformed_signal_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-good")
            signals_dir = core.inbox_root(root) / "signals"
            (signals_dir / "junk.json").write_text("not json", encoding="utf-8")
            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="record",
            )
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["new_signals"][0]["event_id"], "event-good")
        self.assertTrue(
            any(
                "junk.json" in error.get("signal_path", "")
                for error in result["action_errors"]
            )
        )

    def test_watch_survives_scan_errors_and_keeps_heartbeat(self) -> None:
        calls = {"count": 0}

        def flaky_scan(_projects, **_kwargs):
            calls["count"] += 1
            raise OSError("disk hiccup")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            heartbeat = root / "heartbeat.json"
            with contextlib.redirect_stdout(io.StringIO()):
                watcher.watch(
                    [root],
                    state_dir=core.DEFAULT_STATE_DIR,
                    interval_seconds=0.01,
                    state_path=root / "watcher-state.json",
                    action="record",
                    target_thread_id=None,
                    codex="codex",
                    heartbeat_file=heartbeat,
                    max_scans=2,
                    scan=flaky_scan,
                )
            heartbeat_data = core.load_object(heartbeat)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(heartbeat_data["action_error_count"], 1)

    def test_heartbeat_age_never_goes_negative(self) -> None:
        age = watcher.heartbeat_age_seconds(
            {
                "checked_at": (
                    datetime.now(UTC) + timedelta(seconds=5)
                ).isoformat(timespec="milliseconds")
            }
        )
        self.assertEqual(age, 0.0)


class DetectThreadIdTests(unittest.TestCase):
    @staticmethod
    def write_rollout(
        sessions: Path,
        name: str,
        *,
        cwd: str,
        thread_id: str,
        mtime: float,
        originator: str = "Codex Desktop",
    ) -> None:
        path = sessions / "2026" / "07" / "07" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "type": "session_meta",
            "payload": {"id": thread_id, "cwd": cwd, "originator": originator},
        }
        path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))

    def test_env_var_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            detected = codex_app.detect_thread_id(
                root,
                session_roots=[root / "missing"],
                environ={"CODEX_THREAD_ID": "thread-env"},
            )
        self.assertEqual(detected, {"thread_id": "thread-env", "source": "env"})

    def test_picks_newest_interactive_rollout_with_matching_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            project.mkdir()
            sessions = root / "sessions"
            second_store = root / "windows-sessions"
            self.write_rollout(
                sessions,
                "rollout-2026-07-07T10-00-00-old.jsonl",
                cwd=str(project),
                thread_id="thread-old",
                mtime=1000,
            )
            self.write_rollout(
                sessions,
                "rollout-2026-07-07T11-00-00-other.jsonl",
                cwd="/somewhere/else",
                thread_id="thread-other",
                mtime=3000,
            )
            # Headless exec runs are never the chat to wake, even when newest.
            self.write_rollout(
                sessions,
                "rollout-2026-07-07T12-00-00-exec.jsonl",
                cwd=str(project),
                thread_id="thread-exec",
                mtime=4000,
                originator="codex_exec",
            )
            # Desktop sessions may live in a second store (Windows side).
            self.write_rollout(
                second_store,
                "rollout-2026-07-07T10-30-00-new.jsonl",
                cwd=str(project),
                thread_id="thread-new",
                mtime=2000,
            )
            detected = codex_app.detect_thread_id(
                project,
                session_roots=[sessions, second_store],
                environ={},
            )
        self.assertIsNotNone(detected)
        self.assertEqual(detected["thread_id"], "thread-new")

    def test_returns_none_when_nothing_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            detected = codex_app.detect_thread_id(
                root,
                session_roots=[root / "sessions"],
                environ={},
            )
        self.assertIsNone(detected)


class ServiceDiagnosticsTests(unittest.TestCase):
    def start_legacy_service(self, root: Path, thread_id: str = "thread-old"):
        return watcher.start_service(
            [root],
            interval_seconds=5,
            state_path=root / "watcher-state.json",
            service_file=None,
            action="current-thread-callback",
            target_thread_id=thread_id,
            codex="codex",
            popen_factory=FakePopen,
        )

    def test_status_warns_on_stream_host_with_stale_callback_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start_legacy_service(root)
            # Binding switched to a stream-only host after the service was
            # created — exactly the stale mixed state seen in the field.
            binding.write_binding(root, host="claude")
            write_event(root)
            status = watcher.service_status(
                [root],
                process_checker=lambda _pid: False,
            )
        self.assertEqual(status["status"], "crashed")
        self.assertEqual(status["binding_host"], "claude")
        self.assertTrue(
            any("watcher stream" in warning for warning in status["warnings"])
        )
        self.assertTrue(
            any("pending signal" in warning for warning in status["warnings"])
        )

    def test_start_refuses_callback_service_for_stream_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="claude")
            with self.assertRaises(watcher.WatcherError):
                self.start_legacy_service(root)

    def test_status_warns_on_target_thread_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="codex", target_thread_id="thread-new")
            self.start_legacy_service(root, thread_id="thread-old")
            status = watcher.service_status(
                [root],
                process_checker=lambda _pid: False,
            )
        self.assertTrue(
            any("wrong chat" in warning for warning in status["warnings"])
        )

    def test_status_is_quiet_for_matching_codex_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            self.start_legacy_service(root, thread_id="thread-1")
            status = watcher.service_status(
                [root],
                process_checker=lambda _pid: True,
            )
        self.assertEqual(status["binding_host"], "codex")
        self.assertEqual(status["warnings"], [])

    def test_not_started_status_hints_stream_for_pending_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="claude")
            write_event(root)
            status = watcher.service_status([root])
        self.assertEqual(status["status"], "not_started")
        self.assertTrue(
            any("watcher stream" in warning for warning in status["warnings"])
        )


class FakeCompleted:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr


class VscodeChatTests(unittest.TestCase):
    def test_wake_chat_writes_woken_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-vscode")
            signals = core.inbox(root)
            commands: list[list[str]] = []

            def runner(command, **_kwargs):
                commands.append(command)
                return FakeCompleted(0)

            receipt = vscode_chat.wake_chat(root, signals[0], runner=runner)
        self.assertEqual(receipt["status"], "woken")
        self.assertEqual(commands[0][:3], ["code", "chat", "--reuse-window"])
        self.assertIn("LOCAL_AI_ORCHESTRATOR_WAKEUP v1", commands[0][3])

    def test_wake_chat_defers_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-vscode-fail")
            signals = core.inbox(root)
            receipt = vscode_chat.wake_chat(
                root,
                signals[0],
                runner=lambda *_a, **_k: FakeCompleted(1, b"no window"),
            )
        self.assertEqual(receipt["status"], "deferred")
        self.assertIn("no window", receipt["reason"])

    def test_wake_chat_skips_already_woken_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-vscode-dup")
            signals = core.inbox(root)
            first = vscode_chat.wake_chat(
                root,
                signals[0],
                runner=lambda *_a, **_k: FakeCompleted(0),
            )
            second = vscode_chat.wake_chat(
                root,
                signals[0],
                runner=lambda *_a, **_k: FakeCompleted(0),
            )
        self.assertEqual(first["status"], "woken")
        self.assertEqual(second["status"], "skipped")


class CodexActivationTests(unittest.TestCase):
    def test_woken_receipt_includes_activation(self) -> None:
        reset_fake_server("idle")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-activate")
            signals = core.inbox(root)
            receipt = codex_app.wake_current_thread(
                root,
                signals[0],
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
                activator=lambda thread_id: {
                    "activation": "requested",
                    "activation_url": f"codex://threads/{thread_id}",
                },
            )
        self.assertEqual(receipt["status"], "woken")
        self.assertEqual(receipt["activation"], "requested")
        self.assertEqual(receipt["activation_url"], "codex://threads/thread-1")

    def test_activation_failure_does_not_invalidate_woken(self) -> None:
        reset_fake_server("idle")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-activate-fail")
            signals = core.inbox(root)
            receipt = codex_app.wake_current_thread(
                root,
                signals[0],
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
                activator=lambda _thread_id: {
                    "activation": "failed",
                    "activation_error": "boom",
                },
            )
        self.assertEqual(receipt["status"], "woken")
        self.assertEqual(receipt["activation"], "failed")

    def test_wake_current_thread_defers_recent_rollout_activity(self) -> None:
        reset_fake_server("idle")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-recent-direct")
            signals = core.inbox(root)
            receipt = codex_app.wake_current_thread(
                root,
                signals[0],
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
                recent_activity_checker=lambda *_args, **_kwargs: {
                    "rollout_path": "/tmp/rollout.jsonl",
                    "age_seconds": 1.5,
                    "grace_seconds": 90.0,
                },
            )
        self.assertEqual(receipt["status"], "deferred")
        self.assertEqual(receipt["reason"], "thread_recently_active")
        self.assertEqual(receipt["age_seconds"], 1.5)
        self.assertEqual(FakeThreadServer.resumes, 0)
        self.assertEqual(FakeThreadServer.starts, 0)
        self.assertEqual(FakeThreadServer.closes, 1)

    def test_running_turn_hands_off_to_finalizer_without_closing(self) -> None:
        reset_fake_server("idle")
        FakeThreadServer.turn_status = "running"
        captured: dict = {}

        def fake_finalizer(server, *, target_thread_id, turn_id, receipt_path):
            captured.update(
                server=server,
                target_thread_id=target_thread_id,
                turn_id=turn_id,
                receipt_path=receipt_path,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-long-turn")
            signals = core.inbox(root)
            receipt = codex_app.wake_current_thread(
                root,
                signals[0],
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
                activator=lambda _t: {"activation": "requested"},
                finalizer=fake_finalizer,
            )
        self.assertEqual(receipt["status"], "woken")
        self.assertEqual(receipt["turn_status"], "running")
        self.assertEqual(FakeThreadServer.closes, 0)
        self.assertEqual(captured["turn_id"], "turn-1")
        self.assertEqual(captured["target_thread_id"], "thread-1")

    def test_interrupted_turn_is_woken_not_retried(self) -> None:
        reset_fake_server("idle")
        FakeThreadServer.turn_status = "interrupted"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-interrupted")
            signals = core.inbox(root)
            receipt = codex_app.wake_current_thread(
                root,
                signals[0],
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
                activator=lambda _t: {"activation": "requested"},
            )
        self.assertEqual(receipt["status"], "woken")
        self.assertEqual(receipt["turn_status"], "interrupted")
        self.assertEqual(FakeThreadServer.closes, 1)

    def test_completed_turn_closes_server(self) -> None:
        reset_fake_server("idle")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-closed")
            signals = core.inbox(root)
            receipt = codex_app.wake_current_thread(
                root,
                signals[0],
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
                activator=lambda _t: {"activation": "requested"},
            )
        self.assertEqual(receipt["turn_status"], "completed")
        self.assertEqual(FakeThreadServer.closes, 1)

    def test_finalize_turn_updates_receipt_and_closes_server(self) -> None:
        reset_fake_server("idle")
        FakeThreadServer.turn_status = "completed"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            receipt_path = root / "receipt.json"
            core.atomic_json(
                receipt_path,
                {"status": "woken", "turn_status": "running"},
            )
            codex_app.finalize_turn(
                FakeThreadServer(),
                target_thread_id="thread-1",
                turn_id="turn-1",
                receipt_path=receipt_path,
            )
            receipt = core.load_object(receipt_path)
        self.assertEqual(receipt["turn_status"], "completed")
        self.assertIn("finalized_at", receipt)
        self.assertEqual(FakeThreadServer.closes, 1)

    def test_receipt_records_auto_declined_requests(self) -> None:
        reset_fake_server("idle")
        FakeThreadServer.auto_declined = ["execCommandApproval"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, event_id="event-declined")
            signals = core.inbox(root)
            receipt = codex_app.wake_current_thread(
                root,
                signals[0],
                target_thread_id="thread-1",
                server_factory=FakeThreadServer,
                activator=lambda _t: {"activation": "requested"},
            )
        self.assertEqual(receipt["status"], "woken")
        self.assertEqual(
            receipt["auto_declined_requests"],
            ["execCommandApproval"],
        )

    def test_activate_thread_window_builds_deep_link(self) -> None:
        commands: list[list[str]] = []

        def runner(command, **_kwargs):
            commands.append(command)
            return FakeCompleted(0)

        outcome = codex_app.activate_thread_window(
            "thread-7",
            runner=runner,
            alternate_thread_finder=lambda _thread_id: None,
        )
        self.assertEqual(outcome["activation"], "requested")
        self.assertEqual(outcome["activation_strategy"], "single_deep_link")
        self.assertIn("Start-Process 'codex://threads/thread-7'", commands[0][-1])

    def test_activate_thread_window_refreshes_via_alternate_thread(self) -> None:
        commands: list[list[str]] = []
        sleeps: list[float] = []

        def runner(command, **_kwargs):
            commands.append(command)
            return FakeCompleted(0)

        outcome = codex_app.activate_thread_window(
            "thread-target",
            runner=runner,
            alternate_thread_finder=lambda _thread_id: "thread-other",
            sleep=sleeps.append,
            refresh_delay_seconds=0.1,
        )
        self.assertEqual(outcome["activation"], "requested")
        self.assertEqual(outcome["activation_strategy"], "double_deep_link")
        self.assertEqual(outcome["refresh_activation"], "requested")
        self.assertIn(
            "Start-Process 'codex://threads/thread-other'",
            commands[0][-1],
        )
        self.assertIn(
            "Start-Process 'codex://threads/thread-target'",
            commands[1][-1],
        )
        self.assertIn("SendKeys('^r')", commands[2][-1])
        self.assertEqual(sleeps, [0.1])

    def test_activate_thread_window_can_skip_live_refresh(self) -> None:
        commands: list[list[str]] = []

        def runner(command, **_kwargs):
            commands.append(command)
            return FakeCompleted(0)

        outcome = codex_app.activate_thread_window(
            "thread-7",
            runner=runner,
            alternate_thread_finder=lambda _thread_id: None,
            live_refresh=False,
        )
        self.assertEqual(outcome["activation"], "requested")
        self.assertEqual(outcome["live_refresh"], "skipped")
        self.assertEqual(len(commands), 1)

    def test_activate_thread_window_records_live_refresh_failure(self) -> None:
        commands: list[list[str]] = []

        def runner(command, **_kwargs):
            commands.append(command)
            if "SendKeys" in command[-1]:
                return FakeCompleted(2)
            return FakeCompleted(0)

        outcome = codex_app.activate_thread_window(
            "thread-7",
            runner=runner,
            alternate_thread_finder=lambda _thread_id: None,
        )
        self.assertEqual(outcome["activation"], "requested")
        self.assertEqual(outcome["live_refresh"], "failed")
        self.assertEqual(outcome["live_refresh_strategy"], "windows_ctrl_r")
        self.assertIn("exit code 2", outcome["live_refresh_error"])

    def test_activate_thread_window_reports_launcher_failure(self) -> None:
        def runner(*_args, **_kwargs):
            raise OSError("powershell.exe not found")

        outcome = codex_app.activate_thread_window(
            "thread-7",
            runner=runner,
            alternate_thread_finder=lambda _thread_id: None,
        )
        self.assertEqual(outcome["activation"], "failed")
        self.assertIn("not found", outcome["activation_error"])


class AutoDeclineTests(unittest.TestCase):
    def test_build_auto_response_declines_known_requests(self) -> None:
        cases = {
            "execCommandApproval": {"decision": "denied"},
            "applyPatchApproval": {"decision": "denied"},
            "item/commandExecution/requestApproval": {"decision": "decline"},
            "item/fileChange/requestApproval": {"decision": "decline"},
            "mcpServer/elicitation/request": {"action": "decline"},
            "item/tool/requestUserInput": {"answers": {}},
        }
        for method, expected in cases.items():
            response = codex_app.build_auto_response(
                {"id": 7, "method": method, "params": {}}
            )
            self.assertEqual(response["id"], 7)
            self.assertEqual(response["result"], expected)

    def test_build_auto_response_errors_unknown_requests(self) -> None:
        response = codex_app.build_auto_response(
            {"id": 9, "method": "some/unknown/request", "params": {}}
        )
        self.assertEqual(response["id"], 9)
        self.assertNotIn("result", response)
        self.assertIn("auto-declined", response["error"]["message"])

    def test_app_server_auto_declines_live_server_request(self) -> None:
        script = "\n".join(
            [
                "import json, sys",
                "print(json.dumps({",
                "    'id': 7,",
                "    'method': 'execCommandApproval',",
                "    'params': {},",
                "}), flush=True)",
                "line = sys.stdin.readline()",
                "print(json.dumps({",
                "    'method': 'echo',",
                "    'params': {'got': json.loads(line)},",
                "}), flush=True)",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            server = codex_app.AppServer(
                "codex",
                stderr_path=root / "stderr.log",
                command=[sys.executable, "-c", script],
            )
            try:
                echo = server._messages.get(timeout=10)
            finally:
                server.close()
        self.assertEqual(echo["method"], "echo")
        self.assertEqual(echo["params"]["got"]["result"]["decision"], "denied")
        self.assertEqual(server.auto_declined, ["execCommandApproval"])


if __name__ == "__main__":
    unittest.main()
