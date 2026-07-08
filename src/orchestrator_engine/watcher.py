"""Zero-token local watcher and service control."""

from __future__ import annotations

import contextlib
import json
import os
import signal as signal_module
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import binding as binding_module
from . import codex_app, core, vscode_chat

WATCHER_ACTIONS = {"record", "notify", "callback", "current-thread-callback"}
DEFER_BASE_SECONDS = 30
DEFER_MAX_SECONDS = 300
SERVICE_KIND = "LOCAL_AI_ORCHESTRATOR_WATCHER_SERVICE"
MIN_HEARTBEAT_MAX_AGE_SECONDS = 30.0


class WatcherError(RuntimeError):
    """A deterministic watcher failure."""


def _wake_codex(project, signal, *, binding, state_dir, codex, server_factory):
    # Desktop threads live in the Windows-side store; the binding records
    # which codex launcher can actually reach the bound thread.
    bound_codex = binding.get("codex_command") or codex
    return codex_app.wake_current_thread(
        project,
        signal,
        target_thread_id=str(binding["target_thread_id"]),
        state_dir=state_dir,
        codex=bound_codex,
        server_factory=server_factory,
    )


def _wake_vscode(project, signal, *, binding, state_dir, codex, server_factory):
    return vscode_chat.wake_chat(project, signal, state_dir=state_dir)


# Hosts a background watcher can push a wakeup to. The claude host is
# deliberately absent: a Claude session arms its own harness watch on
# `watcher stream`, so a push-style callback service must not consume its
# signals.
HOST_ADAPTERS = {
    "codex": _wake_codex,
    "vscode": _wake_vscode,
}


def resolve_project_binding(
    project: Path,
    *,
    state_dir: str,
    host_adapters: dict | None = None,
) -> dict[str, Any]:
    adapters = HOST_ADAPTERS if host_adapters is None else host_adapters
    bound = binding_module.require_binding(project, state_dir=state_dir)
    host = bound["host"]
    if host not in adapters:
        raise WatcherError(
            f"host {host} does not support callback wakeups; "
            "use `watcher stream` from the host chat instead"
        )
    return bound


def resolve_callback_bindings(
    projects: list[Path],
    *,
    state_dir: str,
    host_adapters: dict | None = None,
) -> dict[Path, dict[str, Any]]:
    return {
        project: resolve_project_binding(
            project,
            state_dir=state_dir,
            host_adapters=host_adapters,
        )
        for project in projects
    }


def callback_binding_for_signal(
    project: Path,
    signal: dict[str, Any],
    *,
    state_dir: str,
    fallback_binding: dict[str, Any] | None,
    host_adapters: dict | None = None,
) -> dict[str, Any]:
    adapters = HOST_ADAPTERS if host_adapters is None else host_adapters
    wake_target = signal.get("wake_target")
    if isinstance(wake_target, dict):
        binding_module.validate_wake_target(wake_target)
        host = wake_target["host"]
        if host not in adapters:
            raise WatcherError(
                f"signal wake target host {host} does not support callback "
                "wakeups; use `watcher stream` from the host chat instead"
            )
        return wake_target
    if fallback_binding is not None:
        return fallback_binding
    return resolve_project_binding(
        project,
        state_dir=state_dir,
        host_adapters=adapters,
    )


def signal_host(
    signal: dict[str, Any],
    *,
    fallback_binding: dict[str, Any] | None,
) -> str | None:
    wake_target = signal.get("wake_target")
    if isinstance(wake_target, dict):
        binding_module.validate_wake_target(wake_target)
        return str(wake_target["host"])
    if fallback_binding is not None:
        return str(fallback_binding["host"])
    return None


def unix_now() -> float:
    return time.time()


def default_state_path(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.inbox_root(project_root, state_dir=state_dir) / "watcher-state.json"


def default_service_path(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.inbox_root(project_root, state_dir=state_dir) / "watcher-service.json"


def default_heartbeat_path(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.inbox_root(project_root, state_dir=state_dir) / "watcher-heartbeat.json"


def default_stream_state_path(
    project_root: Path,
    *,
    host: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return (
        core.inbox_root(project_root, state_dir=state_dir)
        / f"watcher-{host}-stream-state.json"
    )


def default_service_log_path(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return (
        core.inbox_root(project_root, state_dir=state_dir)
        / "logs"
        / "watcher-service.log"
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": core.SCHEMA_VERSION,
            "seen_event_ids": [],
            "deferred_events": {},
        }
    value = core.load_object(path)
    seen = value.get("seen_event_ids")
    if not isinstance(seen, list) or not all(isinstance(item, str) for item in seen):
        raise WatcherError("watcher state has invalid seen_event_ids")
    deferred = value.setdefault("deferred_events", {})
    if not isinstance(deferred, dict) or not all(
        isinstance(key, str) and isinstance(item, dict)
        for key, item in deferred.items()
    ):
        raise WatcherError("watcher state has invalid deferred_events")
    return value


def load_optional_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = core.load_object(path)
    if not isinstance(value, dict):
        raise WatcherError(f"{path} does not contain an object")
    return value


def notify_signal(
    project_root: Path,
    signal: dict[str, Any],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    event_id = str(signal["event_id"])
    root = core.inbox_root(project_root, state_dir=state_dir) / "notifications"
    path = root / f"{event_id}.json"
    core.atomic_json(
        path,
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "LOCAL_AI_ORCHESTRATOR_NOTIFICATION",
            "event_id": event_id,
            "task_id": signal["task_id"],
            "terminal_status": signal["terminal_status"],
            "signal_path": signal.get("signal_path"),
            "created_at": core.utc_now(),
        },
    )
    return path


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def heartbeat_age_seconds(heartbeat: dict[str, Any] | None) -> float | None:
    if not heartbeat:
        return None
    checked_at = heartbeat.get("checked_at")
    if not isinstance(checked_at, str):
        return None
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError:
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - checked).total_seconds(), 0.0)


def heartbeat_max_age_seconds(service_state: dict[str, Any]) -> float:
    interval = service_state.get("interval_seconds")
    if isinstance(interval, (int, float)) and interval > 0:
        return max(float(interval) * 3, MIN_HEARTBEAT_MAX_AGE_SECONDS)
    return MIN_HEARTBEAT_MAX_AGE_SECONDS


def heartbeat_health(
    service_state: dict[str, Any],
    heartbeat: dict[str, Any] | None,
    *,
    alive: bool = True,
) -> dict[str, Any]:
    max_age = heartbeat_max_age_seconds(service_state)
    if not alive:
        return {
            "healthy": False,
            "reason": "not_alive",
            "age_seconds": heartbeat_age_seconds(heartbeat),
            "max_age_seconds": max_age,
        }
    if heartbeat is None:
        return {
            "healthy": False,
            "reason": "missing",
            "age_seconds": None,
            "max_age_seconds": max_age,
        }
    service_pid = service_state.get("pid")
    heartbeat_pid = heartbeat.get("pid")
    if heartbeat_pid != service_pid:
        return {
            "healthy": False,
            "reason": "pid_mismatch",
            "age_seconds": heartbeat_age_seconds(heartbeat),
            "max_age_seconds": max_age,
            "heartbeat_pid": heartbeat_pid,
        }
    age = heartbeat_age_seconds(heartbeat)
    if age is None:
        return {
            "healthy": False,
            "reason": "invalid_timestamp",
            "age_seconds": None,
            "max_age_seconds": max_age,
        }
    if age > max_age:
        return {
            "healthy": False,
            "reason": "stale",
            "age_seconds": age,
            "max_age_seconds": max_age,
        }
    return {
        "healthy": True,
        "reason": "fresh",
        "age_seconds": age,
        "max_age_seconds": max_age,
    }


def write_heartbeat(
    path: Path,
    result: dict[str, Any],
    *,
    action: str,
    interval_seconds: float,
) -> None:
    core.atomic_json(
        path,
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_HEARTBEAT",
            "pid": os.getpid(),
            "checked_at": result["checked_at"],
            "action": action,
            "interval_seconds": interval_seconds,
            "project_roots": result["project_roots"],
            "new_count": result["new_count"],
            "action_error_count": len(result["action_errors"]),
            "state_path": result["state_path"],
        },
    )


def pending_signal_count(
    project_roots: list[Path],
    *,
    state_dir: str,
    state_file: Path,
    host_filter: set[str] | None = None,
    fallback_binding: dict[str, Any] | None = None,
) -> int:
    try:
        state = load_state(state_file)
        seen = set(state["seen_event_ids"])
    except (OSError, RuntimeError, ValueError):
        seen = set()
    count = 0
    for project in project_roots:
        try:
            signals = core.inbox(project, state_dir=state_dir, invalid_sink=[])
        except OSError:
            continue
        for signal in signals:
            event_id = signal.get("event_id")
            if isinstance(event_id, str) and event_id not in seen:
                if host_filter is not None:
                    with contextlib.suppress(RuntimeError, ValueError):
                        host = signal_host(
                            signal,
                            fallback_binding=fallback_binding,
                        )
                        if host not in host_filter:
                            continue
                count += 1
    return count


def scan_once(
    project_roots: list[Path],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    state_path: Path | None = None,
    action: str = "notify",
    target_thread_id: str | None = None,
    codex: str = "codex",
    server_factory=codex_app.AppServer,
    host_adapters: dict | None = None,
    host_filter: set[str] | None = None,
) -> dict[str, Any]:
    if action not in WATCHER_ACTIONS:
        raise WatcherError(f"unsupported watcher action: {action}")
    if action == "current-thread-callback" and not target_thread_id:
        raise WatcherError("target thread id is required for current-thread-callback")

    adapters = HOST_ADAPTERS if host_adapters is None else host_adapters
    projects = [path.expanduser().resolve() for path in project_roots]
    state_file = state_path or default_state_path(
        projects[0],
        state_dir=state_dir,
    )
    state = load_state(state_file)
    seen = set(state["seen_event_ids"])
    deferred_events: dict[str, dict[str, Any]] = state["deferred_events"]
    current_time = unix_now()
    new_signals: list[dict[str, Any]] = []
    notifications: list[str] = []
    thread_wakeups: list[dict[str, Any]] = []
    action_errors: list[dict[str, str]] = []

    for project in projects:
        # A broken fallback binding or an unreadable signal file must degrade
        # to reported errors, never take down a long-running watcher. Signals
        # may carry their own wake_target captured when the task was
        # dispatched; those remain routable even if the project binding later
        # changes.
        bound: dict[str, Any] | None = None
        if action == "callback" or host_filter is not None:
            try:
                bound = binding_module.load_binding(project, state_dir=state_dir)
                if (
                    action == "callback"
                    and bound is not None
                    and bound["host"] not in adapters
                ):
                    # Stream-only binding: signals carrying their own
                    # callback wake_target are still routable; the rest get
                    # a precise per-signal error instead of a per-scan one
                    # (a healthy claude-host project must not spam the log
                    # on every scan).
                    bound = None
            except (OSError, RuntimeError, ValueError) as error:
                action_errors.append(
                    {"project_root": str(project), "error": str(error)}
                )
        invalid_signals: list[dict[str, str]] = []
        signals = core.inbox(
            project,
            state_dir=state_dir,
            invalid_sink=invalid_signals,
        )
        for invalid in invalid_signals:
            action_errors.append({"project_root": str(project), **invalid})
        for signal in signals:
            event_id = signal.get("event_id")
            if not isinstance(event_id, str) or event_id in seen:
                continue
            try:
                host = signal_host(signal, fallback_binding=bound)
            except (OSError, RuntimeError, ValueError) as error:
                action_errors.append(
                    {
                        "event_id": event_id,
                        "project_root": str(project),
                        "error": str(error),
                    }
                )
                continue
            if action == "callback" and host not in adapters:
                continue
            if host_filter is not None and host not in host_filter:
                continue
            deferred = deferred_events.get(event_id)
            retry_after = deferred.get("retry_after_at") if deferred else None
            if isinstance(retry_after, (int, float)) and retry_after > current_time:
                continue
            new_signals.append(signal)
            mark_seen = True
            defer_reason: str | None = None
            try:
                if action == "record":
                    pass
                elif action == "notify":
                    notifications.append(
                        str(
                            notify_signal(
                                project,
                                signal,
                                state_dir=state_dir,
                            )
                        )
                    )
                elif action == "current-thread-callback":
                    wakeup = codex_app.wake_current_thread(
                        project,
                        signal,
                        target_thread_id=str(target_thread_id),
                        state_dir=state_dir,
                        codex=codex,
                        server_factory=server_factory,
                    )
                    thread_wakeups.append(wakeup)
                    if wakeup.get("status") == "deferred":
                        mark_seen = False
                        defer_reason = str(wakeup.get("reason", "deferred"))
                elif action == "callback":
                    event_binding = callback_binding_for_signal(
                        project,
                        signal,
                        state_dir=state_dir,
                        fallback_binding=bound,
                        host_adapters=adapters,
                    )
                    wake = adapters[event_binding["host"]]
                    wakeup = wake(
                        project,
                        signal,
                        binding=event_binding,
                        state_dir=state_dir,
                        codex=codex,
                        server_factory=server_factory,
                    )
                    thread_wakeups.append(wakeup)
                    if wakeup.get("status") == "deferred":
                        mark_seen = False
                        defer_reason = str(wakeup.get("reason", "deferred"))
            except (OSError, RuntimeError, ValueError) as error:
                mark_seen = False
                defer_reason = str(error)
                action_errors.append(
                    {
                        "event_id": event_id,
                        "project_root": str(project),
                        "error": str(error),
                    }
                )
            if mark_seen:
                seen.add(event_id)
                deferred_events.pop(event_id, None)
            elif defer_reason is not None:
                previous = deferred_events.get(event_id, {})
                attempts = int(previous.get("attempts", 0)) + 1
                delay = min(
                    DEFER_BASE_SECONDS * (2 ** (attempts - 1)),
                    DEFER_MAX_SECONDS,
                )
                deferred_events[event_id] = {
                    "attempts": attempts,
                    "reason": defer_reason,
                    "last_attempt_at": current_time,
                    "retry_after_at": current_time + delay,
                }

    output = {
        "schema_version": core.SCHEMA_VERSION,
        "checked_at": core.utc_now(),
        "project_roots": [str(path) for path in projects],
        "new_count": len(new_signals),
        "new_signals": new_signals,
        "notifications": notifications,
        "thread_wakeups": thread_wakeups,
        "action_errors": action_errors,
        "state_path": str(state_file),
    }
    state.update(
        schema_version=core.SCHEMA_VERSION,
        seen_event_ids=sorted(seen),
        deferred_events=deferred_events,
        updated_at=output["checked_at"],
    )
    core.atomic_json(state_file, state)
    return output


def service_status(
    project_roots: list[Path],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    service_file: Path | None = None,
    process_checker=process_alive,
) -> dict[str, Any]:
    projects = [path.expanduser().resolve() for path in project_roots]
    service_path = service_file or default_service_path(
        projects[0],
        state_dir=state_dir,
    )
    state = load_optional_object(service_path)
    heartbeat_path = default_heartbeat_path(
        projects[0],
        state_dir=state_dir,
    )
    heartbeat = load_optional_object(heartbeat_path)
    state_file = default_state_path(projects[0], state_dir=state_dir)
    if state and isinstance(state.get("state_path"), str):
        state_file = Path(state["state_path"])
    bound: dict[str, Any] | None = None
    binding_error: str | None = None
    try:
        bound = binding_module.load_binding(projects[0], state_dir=state_dir)
    except (OSError, RuntimeError, ValueError) as error:
        binding_error = str(error)
    host_filter = None
    if state and state.get("action") == "callback":
        host_filter = set(HOST_ADAPTERS)
    inbox_count = pending_signal_count(
        projects,
        state_dir=state_dir,
        state_file=state_file,
        host_filter=host_filter,
        fallback_binding=bound,
    )
    if not state:
        return {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_SERVICE_STATUS",
            "status": "not_started",
            "alive": False,
            "binding_host": bound.get("host") if bound else None,
            "project_roots": [str(path) for path in projects],
            "service_file": str(service_path),
            "heartbeat_file": str(heartbeat_path),
            "heartbeat_age_seconds": heartbeat_age_seconds(heartbeat),
            "pending_inbox_count": inbox_count,
            "warnings": service_warnings(
                status="not_started",
                state=None,
                bound=bound,
                binding_error=binding_error,
                inbox_count=inbox_count,
            ),
            "checked_at": core.utc_now(),
        }
    pid = state.get("pid")
    alive = isinstance(pid, int) and process_checker(pid)
    heartbeat_status = heartbeat_health(state, heartbeat, alive=alive)
    if alive and heartbeat_status["healthy"]:
        status = "running"
    elif alive:
        status = "degraded"
    elif state.get("status") == "stopped":
        status = "stopped"
    else:
        status = "crashed"
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_SERVICE_STATUS",
        "status": status,
        "alive": alive,
        "pid": pid,
        "process_group": state.get("process_group"),
        "action": state.get("action"),
        "target_thread_id": state.get("target_thread_id"),
        "binding_host": bound.get("host") if bound else None,
        "project_roots": [str(path) for path in projects],
        "service_file": str(service_path),
        "heartbeat_file": str(heartbeat_path),
        "heartbeat_age_seconds": heartbeat_status["age_seconds"],
        "heartbeat_status": heartbeat_status["reason"],
        "heartbeat_healthy": heartbeat_status["healthy"],
        "heartbeat_max_age_seconds": heartbeat_status["max_age_seconds"],
        "log_path": state.get("log_path"),
        "command": state.get("command"),
        "pending_inbox_count": inbox_count,
        "warnings": service_warnings(
            status=status,
            state=state,
            bound=bound,
            binding_error=binding_error,
            inbox_count=inbox_count,
        ),
        "checked_at": core.utc_now(),
    }


CALLBACK_ACTIONS = {"callback", "current-thread-callback"}


def service_warnings(
    *,
    status: str,
    state: dict[str, Any] | None,
    bound: dict[str, Any] | None,
    binding_error: str | None,
    inbox_count: int,
) -> list[str]:
    """Cross-check binding, service action and pending signals.

    A stale or mismatched wake channel must be loud: a crashed service or a
    callback service pointed at the wrong host silently drops wakeups, which
    the user only notices when a finished worker never wakes their chat.
    """
    warnings: list[str] = []
    host = bound.get("host") if bound else None
    if binding_error:
        warnings.append(f"binding is unreadable: {binding_error}")
    action = state.get("action") if state else None
    if (
        state
        and action == "current-thread-callback"
        and host
        and host not in HOST_ADAPTERS
    ):
        warnings.append(
            f"binding host '{host}' wakes via `watcher stream`, but a "
            f"'{action}' service exists; it cannot wake that host — stop the "
            "service (`watcher service stop`) and arm a stream watch from "
            "the host chat"
        )
    if (
        state
        and action == "current-thread-callback"
        and host == "codex"
        and bound is not None
        and state.get("target_thread_id") != bound.get("target_thread_id")
    ):
        warnings.append(
            "service target_thread_id differs from the bound thread; wakeups "
            "would go to the wrong chat — restart the service or rebind"
        )
    if inbox_count:
        if host and host not in HOST_ADAPTERS:
            warnings.append(
                f"{inbox_count} pending signal(s); binding host '{host}' is "
                "delivered by arming `watcher stream` from the host chat"
            )
        elif status in {"crashed", "stopped", "not_started"}:
            warnings.append(
                f"{inbox_count} pending signal(s) will not be delivered "
                "while the watcher is down"
            )
    return warnings


def start_service(
    project_roots: list[Path],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    interval_seconds: float,
    state_path: Path | None,
    service_file: Path | None,
    action: str,
    target_thread_id: str | None,
    codex: str,
    replace: bool = False,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    if interval_seconds <= 0:
        raise WatcherError("interval must be positive")
    if action == "current-thread-callback" and not target_thread_id:
        raise WatcherError("target thread id is required for current-thread-callback")
    projects = [path.expanduser().resolve() for path in project_roots]
    if action == "current-thread-callback":
        # Legacy callback actions ignore the binding at scan time, but
        # starting one against a stream-only host would silently deliver
        # nothing; refuse with the actual fix instead.
        bound = None
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            bound = binding_module.load_binding(projects[0], state_dir=state_dir)
        if bound and bound.get("host") not in HOST_ADAPTERS:
            raise WatcherError(
                f"binding host '{bound['host']}' wakes via `watcher "
                f"stream`; a '{action}' service cannot wake it — arm a "
                "stream watch from the host chat, or rebind with "
                "`bind --host codex --thread-id ...` first"
            )
    service_path = service_file or default_service_path(
        projects[0],
        state_dir=state_dir,
    )
    existing = load_optional_object(service_path)
    existing_pid = existing.get("pid") if existing else None
    if isinstance(existing_pid, int) and process_alive(existing_pid):
        if not replace:
            raise WatcherError(
                f"watcher service is already running with pid {existing_pid}; "
                "use service restart or --replace"
            )
        stop_service(
            projects,
            state_dir=state_dir,
            service_file=service_path,
        )

    watcher_state = state_path or default_state_path(
        projects[0],
        state_dir=state_dir,
    )
    heartbeat_path = default_heartbeat_path(
        projects[0],
        state_dir=state_dir,
    )
    log_path = default_service_log_path(
        projects[0],
        state_dir=state_dir,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "orchestrator_engine.cli"]
    for project in projects:
        command.extend(["--project-root", str(project)])
    command.extend(["--state-dir", state_dir, "watcher"])
    command.extend(
        [
            "--state-file",
            str(watcher_state),
            "--codex",
            codex,
            "--action",
            action,
        ]
    )
    if target_thread_id:
        command.extend(["--target-thread-id", target_thread_id])
    command.extend(
        [
            "watch",
            "--interval-seconds",
            str(interval_seconds),
            "--heartbeat-file",
            str(heartbeat_path),
        ]
    )
    with log_path.open("ab") as log:
        process = popen_factory(
            command,
            cwd=str(projects[0]),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    pid = int(process.pid)
    state = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": SERVICE_KIND,
        "status": "running",
        "pid": pid,
        "process_group": pid,
        "started_at": core.utc_now(),
        "project_roots": [str(path) for path in projects],
        "state_dir": state_dir,
        "action": action,
        "target_thread_id": target_thread_id,
        "interval_seconds": interval_seconds,
        "state_path": str(watcher_state),
        "heartbeat_path": str(heartbeat_path),
        "log_path": str(log_path),
        "command": command,
    }
    core.atomic_json(service_path, state)
    return {**state, "service_file": str(service_path)}


def stop_service(
    project_roots: list[Path],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    service_file: Path | None = None,
    timeout_seconds: float = 5.0,
    process_checker=process_alive,
    kill_group=os.killpg,
) -> dict[str, Any]:
    projects = [path.expanduser().resolve() for path in project_roots]
    service_path = service_file or default_service_path(
        projects[0],
        state_dir=state_dir,
    )
    state = load_optional_object(service_path)
    if not state:
        return {
            "schema_version": core.SCHEMA_VERSION,
            "kind": SERVICE_KIND,
            "status": "not_started",
            "service_file": str(service_path),
            "stopped_at": core.utc_now(),
        }
    if state.get("kind") != SERVICE_KIND:
        raise WatcherError(f"{service_path} is not a watcher service state file")
    pid = state.get("pid")
    process_group = state.get("process_group", pid)
    if not isinstance(pid, int) or not process_checker(pid):
        state.update(
            status="stopped",
            stopped_at=core.utc_now(),
            stop_reason="not_alive",
        )
        core.atomic_json(service_path, state)
        return {**state, "service_file": str(service_path)}
    if not isinstance(process_group, int):
        raise WatcherError("watcher service state has invalid process_group")

    kill_group(process_group, signal_module.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_checker(pid):
            state.update(
                status="stopped",
                stopped_at=core.utc_now(),
                stop_reason="terminated",
            )
            core.atomic_json(service_path, state)
            return {**state, "service_file": str(service_path)}
        time.sleep(0.1)
    kill_group(process_group, signal_module.SIGKILL)
    state.update(status="stopped", stopped_at=core.utc_now(), stop_reason="killed")
    core.atomic_json(service_path, state)
    return {**state, "service_file": str(service_path)}


def watch(
    project_roots: list[Path],
    *,
    state_dir: str,
    interval_seconds: float,
    state_path: Path | None,
    action: str,
    target_thread_id: str | None,
    codex: str,
    heartbeat_file: Path | None = None,
    max_scans: int | None = None,
    scan=scan_once,
) -> None:
    if interval_seconds <= 0:
        raise WatcherError("interval must be positive")
    projects = [path.expanduser().resolve() for path in project_roots]
    heartbeat_path = heartbeat_file or default_heartbeat_path(
        projects[0],
        state_dir=state_dir,
    )
    state_file = state_path or default_state_path(projects[0], state_dir=state_dir)

    def empty_result(errors: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "schema_version": core.SCHEMA_VERSION,
            "checked_at": core.utc_now(),
            "project_roots": [str(path) for path in projects],
            "new_count": 0,
            "new_signals": [],
            "notifications": [],
            "thread_wakeups": [],
            "action_errors": errors,
            "state_path": str(state_file),
        }

    # Wakeups can legitimately take a long time (orchestrator turns may run
    # for hours); a background ticker keeps the heartbeat fresh so service
    # status does not degrade while a scan is busy.
    snapshot: dict[str, Any] = {"result": empty_result([])}
    snapshot_lock = threading.Lock()
    stop_ticker = threading.Event()

    def beat() -> None:
        while not stop_ticker.wait(min(interval_seconds, 10.0)):
            with snapshot_lock:
                result = dict(snapshot["result"])
            result["checked_at"] = core.utc_now()
            try:
                write_heartbeat(
                    heartbeat_path,
                    result,
                    action=action,
                    interval_seconds=interval_seconds,
                )
            except OSError:
                continue

    ticker = threading.Thread(target=beat, name="watcher-heartbeat", daemon=True)
    ticker.start()
    scans = 0
    try:
        while max_scans is None or scans < max_scans:
            try:
                result = scan(
                    projects,
                    state_dir=state_dir,
                    state_path=state_path,
                    action=action,
                    target_thread_id=target_thread_id,
                    codex=codex,
                )
            except (OSError, RuntimeError, ValueError) as error:
                # A single failing scan must not take down a long-running
                # watcher; report it and keep scanning.
                result = empty_result([{"error": str(error)}])
            with snapshot_lock:
                snapshot["result"] = result
            write_heartbeat(
                heartbeat_path,
                result,
                action=action,
                interval_seconds=interval_seconds,
            )
            if result["new_count"] or result["action_errors"]:
                print(
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    flush=True,
                )
            scans += 1
            if max_scans is not None and scans >= max_scans:
                break
            time.sleep(interval_seconds)
    finally:
        stop_ticker.set()
