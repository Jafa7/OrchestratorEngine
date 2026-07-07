from __future__ import annotations

import contextlib
import io
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

    def __init__(self, *_: object, **__: object) -> None:
        pass

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

    def test_callback_without_binding_degrades_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root)
            result = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="callback",
            )
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(len(result["action_errors"]), 1)
        self.assertIn("no binding found", result["action_errors"][0]["error"])

    def test_callback_service_start_requires_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(RuntimeError):
                watcher.start_service(
                    [root],
                    interval_seconds=5,
                    state_path=root / "watcher-state.json",
                    service_file=root / "service.json",
                    action="callback",
                    target_thread_id=None,
                    codex="codex",
                    popen_factory=FakePopen,
                )

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

    def test_callback_rejects_stream_only_host(self) -> None:
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
        self.assertIn("stream", result["action_errors"][0]["error"])

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

        outcome = codex_app.activate_thread_window("thread-7", runner=runner)
        self.assertEqual(outcome["activation"], "requested")
        self.assertIn("Start-Process 'codex://threads/thread-7'", commands[0][-1])

    def test_activate_thread_window_reports_launcher_failure(self) -> None:
        def runner(*_args, **_kwargs):
            raise OSError("powershell.exe not found")

        outcome = codex_app.activate_thread_window("thread-7", runner=runner)
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
