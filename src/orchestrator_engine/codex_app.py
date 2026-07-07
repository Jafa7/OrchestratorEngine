"""Codex App Server adapter for current-thread wakeups."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import core


class CodexAppError(RuntimeError):
    """A deterministic Codex App Server adapter failure."""


class AppServer:
    """Minimal newline-delimited JSON-RPC client for Codex App Server."""

    def __init__(
        self,
        codex: str,
        *,
        stderr_path: Path,
        command: list[str] | None = None,
    ) -> None:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr = stderr_path.open("a", encoding="utf-8")
        self._process = subprocess.Popen(
            command or [codex, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            bufsize=1,
        )
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._request_id = 0

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self._messages.put(value)

    def send(self, value: dict[str, Any]) -> None:
        if self._process.poll() is not None:
            raise CodexAppError("App Server exited before request")
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self._process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60,
    ) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppError(f"App Server {method} deadline elapsed")
            try:
                message = self._messages.get(timeout=min(remaining, 1))
            except queue.Empty:
                if self._process.poll() is not None:
                    raise CodexAppError(
                        f"App Server exited with code {self._process.returncode}"
                    ) from None
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CodexAppError(
                    f"App Server {method} failed: "
                    f"{json.dumps(message['error'], ensure_ascii=False)}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexAppError(f"App Server {method} returned invalid result")
            return result

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._stderr.close()

    def __enter__(self) -> AppServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def thread_status_type(response: dict[str, Any]) -> str | None:
    thread = response.get("thread")
    if not isinstance(thread, dict):
        return None
    status = thread.get("status")
    if isinstance(status, dict) and isinstance(status.get("type"), str):
        return status["type"]
    if isinstance(status, str):
        return status
    return None


def thread_wakeup_receipt_path(
    project_root: Path,
    event_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return (
        core.inbox_root(project_root, state_dir=state_dir)
        / "thread-wakeups"
        / f"{event_id}.json"
    )


def build_current_thread_wakeup_message(
    project_root: Path,
    signal: dict[str, Any],
    event: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "LOCAL_AI_ORCHESTRATOR_WAKEUP v1",
            f"project: {project_root}",
            f"event_id: {event['event_id']}",
            f"task_id: {event['task_id']}",
            f"terminal_status: {event['terminal_status']}",
            f"event: {signal['event_path']}",
            f"evidence: {event['evidence_path']}",
            f"result: {event['result_path']}",
            "requires: ORCHESTRATOR_FOLLOWUP",
            "",
            "Read the event/evidence. Verify state and decide the next safe action.",
            "If review is required, inspect the real diff and checks before accepting.",
            "Do not commit or push unless the user explicitly requested it.",
        ]
    )


def wake_current_thread(
    project_root: Path,
    signal: dict[str, Any],
    *,
    target_thread_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    codex: str = "codex",
    server_factory=AppServer,
) -> dict[str, Any]:
    if not target_thread_id:
        raise CodexAppError("target thread id is required")
    event_id = signal.get("event_id")
    event_path_value = signal.get("event_path")
    if not isinstance(event_id, str) or not event_id:
        raise CodexAppError("signal has invalid event_id")
    if not isinstance(event_path_value, str) or not event_path_value:
        raise CodexAppError("signal has invalid event_path")

    project = project_root.expanduser().resolve()
    receipt_path = thread_wakeup_receipt_path(project, event_id, state_dir=state_dir)
    if receipt_path.exists():
        existing = core.load_object(receipt_path)
        if existing.get("status") == "woken":
            return {
                "schema_version": core.SCHEMA_VERSION,
                "event_id": event_id,
                "status": "skipped",
                "reason": "already_woken",
                "receipt": str(receipt_path),
            }

    event_path = Path(event_path_value).expanduser().resolve()
    event = core.verify_terminal_event(event_path)
    log_path = (
        core.inbox_root(project, state_dir=state_dir)
        / "logs"
        / f"{event_id}.thread-wakeup.app-server.log"
    )
    try:
        with server_factory(codex, stderr_path=log_path) as server:
            server.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "orchestrator-engine-current-thread-watcher",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            )
            server.notify("initialized")
            thread_read = server.request(
                "thread/read",
                {"threadId": target_thread_id, "includeTurns": False},
            )
            status = thread_status_type(thread_read)
            if status == "active":
                receipt = {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": "CURRENT_THREAD_WAKEUP",
                    "event_id": event_id,
                    "task_id": event["task_id"],
                    "target_thread_id": target_thread_id,
                    "status": "deferred",
                    "reason": "thread_active",
                    "created_at": core.utc_now(),
                }
                core.atomic_json(receipt_path, receipt)
                return {**receipt, "receipt": str(receipt_path)}
            if status not in {"idle", "notLoaded"}:
                receipt = {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": "CURRENT_THREAD_WAKEUP",
                    "event_id": event_id,
                    "task_id": event["task_id"],
                    "target_thread_id": target_thread_id,
                    "status": "deferred",
                    "reason": f"thread_status_{status or 'unknown'}",
                    "created_at": core.utc_now(),
                }
                core.atomic_json(receipt_path, receipt)
                return {**receipt, "receipt": str(receipt_path)}
            server.request("thread/resume", {"threadId": target_thread_id})
            turn = server.request(
                "turn/start",
                {
                    "threadId": target_thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": build_current_thread_wakeup_message(
                                project,
                                signal,
                                event,
                            ),
                        }
                    ],
                },
            )
    except (OSError, RuntimeError, ValueError) as error:
        receipt = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "CURRENT_THREAD_WAKEUP",
            "event_id": event_id,
            "target_thread_id": target_thread_id,
            "status": "deferred",
            "reason": str(error),
            "created_at": core.utc_now(),
        }
        core.atomic_json(receipt_path, receipt)
        return {**receipt, "receipt": str(receipt_path)}

    turn_value = turn.get("turn")
    turn_id = turn_value.get("id") if isinstance(turn_value, dict) else None
    receipt = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "CURRENT_THREAD_WAKEUP",
        "event_id": event_id,
        "task_id": event["task_id"],
        "target_thread_id": target_thread_id,
        "status": "woken",
        "turn_id": turn_id,
        "created_at": core.utc_now(),
    }
    core.atomic_json(receipt_path, receipt)
    return {**receipt, "receipt": str(receipt_path)}
