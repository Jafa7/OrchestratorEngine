"""Codex App Server adapter for current-thread wakeups."""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import core, wakeup


class CodexAppError(RuntimeError):
    """A deterministic Codex App Server adapter failure."""


# How long to watch a freshly started turn for an immediate failure (rate
# limits and validation errors surface within seconds). Turns still running
# after the window are handed to a background finalizer with no deadline, so
# arbitrarily long orchestrator turns never block or break the watcher.
TURN_FAILURE_WINDOW_SECONDS = 120
FINALIZER_POLL_WINDOW_SECONDS = 3600

# Server->client requests raised by an injected turn (command approvals,
# patch approvals, elicitations) would otherwise wait forever for a human who
# cannot see them. They are answered with the protocol's decline decision —
# never auto-approved — so the turn continues and ends with a text answer
# instead of hanging. Declines are recorded in the wakeup receipt.
AUTO_DECLINE_RESULTS: dict[str, dict[str, Any]] = {
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "mcpServer/elicitation/request": {"action": "decline"},
    "item/tool/requestUserInput": {"answers": {}},
}


def build_auto_response(message: dict[str, Any]) -> dict[str, Any]:
    method = message.get("method")
    request_id = message.get("id")
    result = AUTO_DECLINE_RESULTS.get(str(method))
    if result is not None:
        return {"id": request_id, "result": result}
    return {
        "id": request_id,
        "error": {
            "code": -32601,
            "message": (
                f"{method} auto-declined by orchestrator-engine watcher"
            ),
        },
    }
DEEP_LINK_COMMAND = [
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
]


def activate_thread_window(
    thread_id: str,
    *,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Bring the Codex Desktop thread to the foreground via its deep link.

    The injected turn lands in shared thread storage, but the live window is a
    separate process that does not refresh on its own; the `codex://threads/`
    deep link is how the desktop app opens a specific thread.
    """
    url = f"codex://threads/{thread_id}"
    try:
        completed = runner(
            [*DEEP_LINK_COMMAND, f"Start-Process '{url}'"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"activation": "failed", "activation_error": str(error)}
    if completed.returncode != 0:
        return {
            "activation": "failed",
            "activation_error": f"exit code {completed.returncode}",
        }
    return {"activation": "requested", "activation_url": url}


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
        self._send_lock = threading.Lock()
        self.auto_declined: list[str] = []
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
            if not isinstance(value, dict):
                continue
            if "method" in value and "id" in value:
                # Server->client request: nobody is here to answer it, so
                # decline it immediately instead of letting the turn hang.
                self._respond_to_server_request(value)
                continue
            self._messages.put(value)

    def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        try:
            self.send(build_auto_response(message))
        except (OSError, RuntimeError, ValueError):
            return
        self.auto_declined.append(str(message.get("method")))

    def send(self, value: dict[str, Any]) -> None:
        if self._process.poll() is not None:
            raise CodexAppError("App Server exited before request")
        assert self._process.stdin is not None
        with self._send_lock:
            self._process.stdin.write(
                json.dumps(value, separators=(",", ":")) + "\n"
            )
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

    def await_turn_outcome(
        self,
        thread_id: str,
        turn_id: str,
        *,
        window_seconds: float,
    ) -> dict[str, Any] | None:
        """Wait up to window_seconds for the turn to finish.

        Returns the final turn object, or None when the turn is still
        running at the end of the window. `turn/start` only acknowledges
        that a turn was accepted; failures (including rate limits) surface
        later as a `turn/completed` notification with a non-completed
        status.
        """
        deadline = time.monotonic() + window_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                message = self._messages.get(timeout=min(remaining, 1))
            except queue.Empty:
                if self._process.poll() is not None:
                    raise CodexAppError(
                        f"App Server exited with code {self._process.returncode}"
                    ) from None
                continue
            if message.get("method") != "turn/completed":
                continue
            params = message.get("params")
            if not isinstance(params, dict) or params.get("threadId") != thread_id:
                continue
            turn = params.get("turn")
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return turn

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        self._stderr.close()

    def __enter__(self) -> AppServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def finalize_turn(
    server: AppServer,
    *,
    target_thread_id: str,
    turn_id: str,
    receipt_path: Path,
) -> None:
    """Follow a still-running turn to completion and record its outcome.

    Runs with no overall deadline: orchestrator turns may legitimately take
    hours. The server connection stays open (and drained) until the turn
    ends, because closing it would abort the in-flight turn.
    """
    turn_status = "unknown"
    error: str | None = None
    try:
        while True:
            outcome = server.await_turn_outcome(
                target_thread_id,
                turn_id,
                window_seconds=FINALIZER_POLL_WINDOW_SECONDS,
            )
            if outcome is not None:
                turn_status = str(outcome.get("status", "unknown"))
                turn_error = outcome.get("error")
                if isinstance(turn_error, dict) and turn_error.get("message"):
                    error = str(turn_error["message"])
                break
    except (OSError, RuntimeError, ValueError) as exc:
        error = str(exc)
    finally:
        server.close()
    try:
        receipt = core.load_object(receipt_path)
    except (OSError, RuntimeError, ValueError):
        receipt = {}
    receipt.update(turn_status=turn_status, finalized_at=core.utc_now())
    if error:
        receipt["turn_error"] = error
    declined = list(getattr(server, "auto_declined", []))
    if declined:
        receipt["auto_declined_requests"] = declined
    core.atomic_json(receipt_path, receipt)


def spawn_turn_finalizer(
    server: AppServer,
    *,
    target_thread_id: str,
    turn_id: str,
    receipt_path: Path,
) -> threading.Thread:
    thread = threading.Thread(
        target=finalize_turn,
        args=(server,),
        kwargs={
            "target_thread_id": target_thread_id,
            "turn_id": turn_id,
            "receipt_path": receipt_path,
        },
        name=f"turn-finalizer-{turn_id}",
    )
    thread.start()
    return thread


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
    return wakeup.build_wakeup_message(project_root, signal, event)


def wake_current_thread(
    project_root: Path,
    signal: dict[str, Any],
    *,
    target_thread_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    codex: str = "codex",
    server_factory=AppServer,
    activator=activate_thread_window,
    failure_window_seconds: float = TURN_FAILURE_WINDOW_SECONDS,
    finalizer=spawn_turn_finalizer,
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
    receipt_path = thread_wakeup_receipt_path(
        project,
        event_id,
        state_dir=state_dir,
    )
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
    server = None
    handed_off = False
    try:
        server = server_factory(codex, stderr_path=log_path)
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
        started = server.request(
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
        started_turn = started.get("turn")
        turn_id = (
            started_turn.get("id") if isinstance(started_turn, dict) else None
        )
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexAppError("turn/start returned no turn id")
        outcome = server.await_turn_outcome(
            target_thread_id,
            turn_id,
            window_seconds=failure_window_seconds,
        )
        if outcome is not None and outcome.get("status") == "failed":
            turn_error = outcome.get("error")
            message = (
                turn_error.get("message")
                if isinstance(turn_error, dict)
                else None
            )
            raise CodexAppError(f"turn failed: {message or 'no error detail'}")
        # A turn still running after the failure window was delivered; it may
        # legitimately run for hours, so it is finalized in the background.
        turn_status = (
            "running" if outcome is None else str(outcome.get("status", "unknown"))
        )
        handed_off = outcome is None
        declined_so_far = list(getattr(server, "auto_declined", []))
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
    finally:
        if server is not None and not handed_off:
            server.close()

    try:
        activation = activator(target_thread_id)
        receipt = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "CURRENT_THREAD_WAKEUP",
            "event_id": event_id,
            "task_id": event["task_id"],
            "target_thread_id": target_thread_id,
            "status": "woken",
            "turn_id": turn_id,
            "turn_status": turn_status,
            "created_at": core.utc_now(),
            **activation,
        }
        if declined_so_far:
            receipt["auto_declined_requests"] = declined_so_far
        core.atomic_json(receipt_path, receipt)
        if handed_off:
            finalizer(
                server,
                target_thread_id=target_thread_id,
                turn_id=turn_id,
                receipt_path=receipt_path,
            )
    except BaseException:
        if handed_off:
            server.close()
        raise
    return {**receipt, "receipt": str(receipt_path)}
