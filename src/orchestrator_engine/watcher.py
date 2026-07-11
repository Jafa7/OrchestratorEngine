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
DEFER_MAX_ATTEMPTS = 5
DEFER_STATUS_RETRYABLE = "deferred_retryable"
DEFER_STATUS_MANUAL_REQUIRED = "deferred_manual_required"
ACKNOWLEDGED_STATUS = "acknowledged"
RETRYABLE_GUARD_REASON_CODES = {"thread_active", "thread_recently_active"}
QUOTA_REASON_MARKERS = (
    "usage limit",
    "rate limit",
    "quota",
    "purchase more credits",
    "try again at",
)
SERVICE_KIND = "LOCAL_AI_ORCHESTRATOR_WATCHER_SERVICE"
STATE_KIND = "LOCAL_AI_ORCHESTRATOR_WATCHER_STATE"
ACKNOWLEDGEMENT_KIND = "LOCAL_AI_ORCHESTRATOR_WATCHER_ACKNOWLEDGEMENT"
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


def default_host_state_path(
    project_root: Path,
    *,
    host: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    """Return the state file that owns delivery for one host."""

    if host == "claude":
        return default_stream_state_path(
            project_root,
            host=host,
            state_dir=state_dir,
        )
    return default_callback_state_path(
        project_root,
        host=host,
        state_dir=state_dir,
    )


def acknowledgement_receipt_path(
    project_root: Path,
    *,
    host: str,
    event_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    if not event_id or "/" in event_id or "\\" in event_id or event_id.startswith("."):
        raise WatcherError(f"invalid event id: {event_id!r}")
    return (
        core.inbox_root(project_root, state_dir=state_dir)
        / "acknowledgements"
        / host
        / f"{event_id}.json"
    )


def default_callback_state_path(
    project_root: Path,
    *,
    host: str | None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    if host is None:
        return default_state_path(project_root, state_dir=state_dir)
    return (
        core.inbox_root(project_root, state_dir=state_dir)
        / f"watcher-{host}-callback-state.json"
    )


def default_callback_service_path(
    project_root: Path,
    *,
    host: str | None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    if host is None:
        return default_service_path(project_root, state_dir=state_dir)
    return (
        core.inbox_root(project_root, state_dir=state_dir)
        / f"watcher-{host}-callback-service.json"
    )


def default_callback_heartbeat_path(
    project_root: Path,
    *,
    host: str | None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    if host is None:
        return default_heartbeat_path(project_root, state_dir=state_dir)
    return (
        core.inbox_root(project_root, state_dir=state_dir)
        / f"watcher-{host}-callback-heartbeat.json"
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
            "kind": STATE_KIND,
            "seen_event_ids": [],
            "deferred_events": {},
            "acknowledged_events": {},
        }
    value = core.load_object(path)
    kind = value.get("kind")
    if kind not in {None, STATE_KIND}:
        raise WatcherError("watcher state has invalid kind")
    if not core.is_supported_schema_version(value.get("schema_version")):
        raise WatcherError("watcher state has unsupported schema")
    seen = value.get("seen_event_ids")
    if not isinstance(seen, list) or not all(isinstance(item, str) for item in seen):
        raise WatcherError("watcher state has invalid seen_event_ids")
    deferred = value.setdefault("deferred_events", {})
    if not isinstance(deferred, dict) or not all(
        isinstance(key, str) and isinstance(item, dict)
        for key, item in deferred.items()
    ):
        raise WatcherError("watcher state has invalid deferred_events")
    acknowledged = value.setdefault("acknowledged_events", {})
    if not isinstance(acknowledged, dict) or not all(
        isinstance(key, str) and isinstance(item, dict)
        for key, item in acknowledged.items()
    ):
        raise WatcherError("watcher state has invalid acknowledged_events")
    return value


def load_optional_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = core.load_object(path)
    if not isinstance(value, dict):
        raise WatcherError(f"{path} does not contain an object")
    return value


def seed_state_from_legacy(
    project_root: Path,
    *,
    state_file: Path,
    state_dir: str,
) -> None:
    legacy = default_state_path(project_root, state_dir=state_dir)
    if state_file.exists() or state_file == legacy or not legacy.exists():
        return
    legacy_state = load_state(legacy)
    core.atomic_json(
        state_file,
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": STATE_KIND,
            "seen_event_ids": list(legacy_state["seen_event_ids"]),
            "deferred_events": {},
            "acknowledged_events": {},
            "seeded_from": str(legacy),
            "seeded_at": core.utc_now(),
        },
    )


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


def deferred_status(item: dict[str, Any]) -> str:
    status = item.get("status")
    if status in {DEFER_STATUS_RETRYABLE, DEFER_STATUS_MANUAL_REQUIRED}:
        return str(status)
    return DEFER_STATUS_RETRYABLE


def defer_reason_code(reason: str) -> str:
    normalized = reason.strip().lower()
    if normalized in RETRYABLE_GUARD_REASON_CODES:
        return normalized
    if any(marker in normalized for marker in QUOTA_REASON_MARKERS):
        return "quota_or_usage_limit"
    return "callback_failed"


def deferred_operator_action(status: str, reason_code: str) -> str:
    if status == DEFER_STATUS_MANUAL_REQUIRED:
        if reason_code == "quota_or_usage_limit":
            return (
                "Read the event/result/evidence manually, then acknowledge "
                "the event or retry after quota resets."
            )
        return (
            "Inspect the callback failure, read event/result/evidence if "
            "needed, then acknowledge the event or restart the delivery channel."
        )
    return "Watcher will retry after next_retry_at unless the event is acknowledged."


def build_deferred_record(
    event_id: str,
    signal: dict[str, Any],
    *,
    reason: str,
    previous: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    attempts = int(previous.get("attempts", 0)) + 1
    reason_code = defer_reason_code(reason)
    manual_required = (
        reason_code == "quota_or_usage_limit"
        or (
            reason_code not in RETRYABLE_GUARD_REASON_CODES
            and attempts >= DEFER_MAX_ATTEMPTS
        )
    )
    status = (
        DEFER_STATUS_MANUAL_REQUIRED if manual_required else DEFER_STATUS_RETRYABLE
    )
    record: dict[str, Any] = {
        "status": status,
        "attempts": attempts,
        "reason": reason,
        "reason_code": reason_code,
        "event_id": event_id,
        "task_id": signal.get("task_id"),
        "terminal_status": signal.get("terminal_status"),
        "event_path": signal.get("event_path"),
        "signal_path": signal.get("signal_path"),
        "first_attempt_at": previous.get("first_attempt_at", now),
        "last_attempt_at": now,
        "operator_action": deferred_operator_action(status, reason_code),
    }
    if status == DEFER_STATUS_RETRYABLE:
        delay = min(
            DEFER_BASE_SECONDS * (2 ** (attempts - 1)),
            DEFER_MAX_SECONDS,
        )
        record["retry_after_at"] = now + delay
    return record


def unix_timestamp_to_iso(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(float(value), tz=UTC).isoformat(
        timespec="milliseconds"
    )


def deferred_event_summaries(
    project_roots: list[Path],
    *,
    state_dir: str,
    state_file: Path,
) -> list[dict[str, Any]]:
    try:
        state = load_state(state_file)
    except (OSError, RuntimeError, ValueError):
        return []
    signal_index: dict[str, dict[str, Any]] = {}
    for project in project_roots:
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            for signal in core.inbox(project, state_dir=state_dir, invalid_sink=[]):
                event_id = signal.get("event_id")
                if isinstance(event_id, str):
                    signal_index[event_id] = signal
    summaries: list[dict[str, Any]] = []
    for event_id, item in sorted(state["deferred_events"].items()):
        signal = signal_index.get(event_id, {})
        retry_after_at = item.get("retry_after_at")
        status = deferred_status(item)
        summaries.append(
            {
                "event_id": event_id,
                "task_id": item.get("task_id") or signal.get("task_id"),
                "terminal_status": item.get("terminal_status")
                or signal.get("terminal_status"),
                "status": status,
                "attempts": int(item.get("attempts", 0)),
                "last_reason": item.get("reason"),
                "reason_code": item.get("reason_code")
                or defer_reason_code(str(item.get("reason", ""))),
                "first_attempt_at": item.get("first_attempt_at"),
                "first_attempt_at_iso": unix_timestamp_to_iso(
                    item.get("first_attempt_at")
                ),
                "last_attempt_at": item.get("last_attempt_at"),
                "last_attempt_at_iso": unix_timestamp_to_iso(
                    item.get("last_attempt_at")
                ),
                "retry_after_at": retry_after_at,
                "next_retry_at": unix_timestamp_to_iso(retry_after_at),
                "event_path": item.get("event_path") or signal.get("event_path"),
                "signal_path": item.get("signal_path") or signal.get("signal_path"),
                "operator_action": item.get("operator_action")
                or deferred_operator_action(
                    status,
                    str(item.get("reason_code") or "callback_failed"),
                ),
            }
        )
    return summaries


def deferred_status_counts(
    summaries: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {
        DEFER_STATUS_RETRYABLE: 0,
        DEFER_STATUS_MANUAL_REQUIRED: 0,
    }
    for summary in summaries:
        status = summary.get("status")
        if status in counts:
            counts[str(status)] += 1
    return counts


def list_deferred_events(
    project_roots: list[Path],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    state_path: Path | None = None,
) -> dict[str, Any]:
    projects = [path.expanduser().resolve() for path in project_roots]
    state_file = state_path or default_state_path(projects[0], state_dir=state_dir)
    summaries = deferred_event_summaries(
        projects,
        state_dir=state_dir,
        state_file=state_file,
    )
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_DEFERRED_LIST",
        "project_roots": [str(path) for path in projects],
        "state_path": str(state_file),
        "deferred_event_count": len(summaries),
        "deferred_status_counts": deferred_status_counts(summaries),
        "deferred_events": summaries,
        "checked_at": core.utc_now(),
    }


def retry_deferred_event(
    project_root: Path,
    *,
    event_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    state_path: Path | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    if not event_id:
        raise WatcherError("event id is required")
    project = project_root.expanduser().resolve()
    state_file = state_path or default_state_path(project, state_dir=state_dir)
    state = load_state(state_file)
    deferred_events: dict[str, dict[str, Any]] = state["deferred_events"]
    previous = deferred_events.get(event_id)
    if previous is None:
        if event_id in set(state["seen_event_ids"]):
            raise WatcherError(f"event is already seen: {event_id}")
        raise WatcherError(f"event is not deferred: {event_id}")
    previous_status = deferred_status(previous)
    previous_retry_after = previous.get("retry_after_at")
    requested_at = core.utc_now()
    previous["status"] = DEFER_STATUS_RETRYABLE
    previous.pop("retry_after_at", None)
    previous["retry_requested_at"] = requested_at
    previous["retry_previous_status"] = previous_status
    if reason:
        previous["retry_reason"] = reason
    previous["operator_action"] = deferred_operator_action(
        DEFER_STATUS_RETRYABLE,
        str(previous.get("reason_code") or "callback_failed"),
    )
    state["deferred_events"] = deferred_events
    state["updated_at"] = requested_at
    core.atomic_json(state_file, state)
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_DEFERRED_RETRY",
        "event_id": event_id,
        "task_id": previous.get("task_id"),
        "status": "retry_scheduled",
        "previous_status": previous_status,
        "new_status": DEFER_STATUS_RETRYABLE,
        "previous_retry_after_at": previous_retry_after,
        "retry_after_at": None,
        "retry_requested_at": requested_at,
        "state_path": str(state_file),
        "operator_action": previous["operator_action"],
    }


def acknowledge_signal(
    project_root: Path,
    *,
    event_id: str,
    host: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    state_path: Path | None = None,
    reason: str,
    allow_unbound_signal: bool = False,
) -> dict[str, Any]:
    if not event_id:
        raise WatcherError("event id is required")
    if host not in binding_module.SUPPORTED_HOSTS:
        raise WatcherError(f"unsupported host: {host}")
    if not isinstance(reason, str) or not reason.strip():
        raise WatcherError("acknowledgement reason is required")
    project = project_root.expanduser().resolve()
    state_file = state_path or default_host_state_path(
        project, host=host, state_dir=state_dir
    )
    receipt_path = acknowledgement_receipt_path(
        project,
        host=host,
        event_id=event_id,
        state_dir=state_dir,
    )
    existing_receipt = load_optional_object(receipt_path)
    state = load_state(state_file)
    seen = set(state["seen_event_ids"])
    deferred_events: dict[str, dict[str, Any]] = state["deferred_events"]
    previous = deferred_events.get(event_id)
    signal = None
    fallback_binding = binding_module.load_binding(project, state_dir=state_dir)
    for candidate in core.inbox(project, state_dir=state_dir, invalid_sink=[]):
        if candidate.get("event_id") != event_id:
            continue
        signal_host_value = signal_host(candidate, fallback_binding=fallback_binding)
        if signal_host_value == host or (
            allow_unbound_signal and signal_host_value is None
        ):
            signal = candidate
            break
    if existing_receipt is not None:
        if (
            not core.is_supported_schema_version(existing_receipt.get("schema_version"))
            or existing_receipt.get("kind") != ACKNOWLEDGEMENT_KIND
            or existing_receipt.get("event_id") != event_id
            or existing_receipt.get("host") != host
            or existing_receipt.get("status") != ACKNOWLEDGED_STATUS
        ):
            raise WatcherError(f"invalid acknowledgement receipt: {receipt_path}")
        acknowledgement = existing_receipt
    elif previous is None and signal is None and event_id not in seen:
        raise WatcherError(f"event is not pending or deferred: {event_id}")
    else:
        acknowledged_at = core.utc_now()
        acknowledgement = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": ACKNOWLEDGEMENT_KIND,
            "event_id": event_id,
            "host": host,
            "task_id": (previous or {}).get("task_id")
            or (signal or {}).get("task_id"),
            "status": ACKNOWLEDGED_STATUS,
            "reason": reason.strip(),
            "acknowledged_at": acknowledged_at,
            "previous_status": deferred_status(previous) if previous else "pending",
            "previous_attempts": int((previous or {}).get("attempts", 0)),
            "previous_deferred": previous,
            "state_path": str(state_file),
            "receipt_path": str(receipt_path),
        }
        core.atomic_json(receipt_path, acknowledgement)
    seen.add(event_id)
    state["seen_event_ids"] = sorted(seen)
    deferred_events.pop(event_id, None)
    state["deferred_events"] = deferred_events
    state["acknowledged_events"][event_id] = acknowledgement
    state["schema_version"] = core.SCHEMA_VERSION
    state["kind"] = STATE_KIND
    state["updated_at"] = core.utc_now()
    core.atomic_json(state_file, state)
    return {**acknowledgement, "idempotent": existing_receipt is not None}


def acknowledge_deferred_event(
    project_root: Path,
    *,
    event_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    state_path: Path | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for the historical unscoped API.

    New CLI workflows call :func:`acknowledge_signal` with an explicit host.
    """

    return acknowledge_signal(
        project_root,
        event_id=event_id,
        host="codex",
        state_dir=state_dir,
        state_path=state_path,
        reason=reason or "legacy manual acknowledgement",
        allow_unbound_signal=True,
    )


def acknowledge_pending_signals(
    project_root: Path,
    *,
    host: str,
    reason: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Acknowledge every currently pending signal for one host, explicitly."""

    project = project_root.expanduser().resolve()
    state_file = state_path or default_host_state_path(
        project, host=host, state_dir=state_dir
    )
    state = load_state(state_file)
    seen = set(state["seen_event_ids"])
    fallback_binding = binding_module.load_binding(project, state_dir=state_dir)
    event_ids = []
    for signal in core.inbox(project, state_dir=state_dir, invalid_sink=[]):
        event_id = signal.get("event_id")
        if not isinstance(event_id, str) or event_id in seen:
            continue
        if signal_host(signal, fallback_binding=fallback_binding) == host:
            event_ids.append(event_id)
    receipts = [
        acknowledge_signal(
            project,
            event_id=event_id,
            host=host,
            reason=reason,
            state_dir=state_dir,
            state_path=state_file,
        )
        for event_id in sorted(event_ids)
    ]
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_ACKNOWLEDGEMENT_BULK",
        "host": host,
        "status": ACKNOWLEDGED_STATUS,
        "reason": reason.strip(),
        "acknowledged_count": len(receipts),
        "acknowledgements": receipts,
        "state_path": str(state_file),
    }


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
    seed_state_from_legacy(projects[0], state_file=state_file, state_dir=state_dir)
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
            if (
                deferred is not None
                and deferred_status(deferred) == DEFER_STATUS_MANUAL_REQUIRED
            ):
                continue
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
                deferred_events[event_id] = build_deferred_record(
                    event_id,
                    signal,
                    reason=defer_reason,
                    previous=previous,
                    now=current_time,
                )

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
        kind=STATE_KIND,
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
    host: str | None = None,
    process_checker=process_alive,
) -> dict[str, Any]:
    projects = [path.expanduser().resolve() for path in project_roots]
    service_path = service_file or default_callback_service_path(
        projects[0],
        host=host,
        state_dir=state_dir,
    )
    state = load_optional_object(service_path)
    heartbeat_path = default_callback_heartbeat_path(
        projects[0],
        host=host,
        state_dir=state_dir,
    )
    heartbeat = load_optional_object(heartbeat_path)
    state_file = default_callback_state_path(
        projects[0],
        host=host,
        state_dir=state_dir,
    )
    if state and isinstance(state.get("state_path"), str):
        state_file = Path(state["state_path"])
    bound: dict[str, Any] | None = None
    binding_error: str | None = None
    try:
        bound = binding_module.load_binding(projects[0], state_dir=state_dir)
    except (OSError, RuntimeError, ValueError) as error:
        binding_error = str(error)
    host_filter = {host} if host else None
    stored_filter = state.get("host_filter") if state else None
    if isinstance(stored_filter, list) and all(
        isinstance(item, str) for item in stored_filter
    ):
        host_filter = set(stored_filter)
    elif state and state.get("action") == "callback":
        host_filter = set(HOST_ADAPTERS)
    inbox_count = pending_signal_count(
        projects,
        state_dir=state_dir,
        state_file=state_file,
        host_filter=host_filter,
        fallback_binding=bound,
    )
    bound_host = bound.get("host") if bound else None
    host_scoped_pending_count: int | None = None
    if host is None and isinstance(bound_host, str) and bound_host in HOST_ADAPTERS:
        host_scoped_pending_count = pending_signal_count(
            projects,
            state_dir=state_dir,
            state_file=default_callback_state_path(
                projects[0],
                host=bound_host,
                state_dir=state_dir,
            ),
            host_filter={bound_host},
            fallback_binding=bound,
        )
    deferred_events = deferred_event_summaries(
        projects,
        state_dir=state_dir,
        state_file=state_file,
    )
    deferred_counts = deferred_status_counts(deferred_events)
    if not state:
        return {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "LOCAL_AI_ORCHESTRATOR_WATCHER_SERVICE_STATUS",
            "status": "not_started",
            "alive": False,
            "binding_host": bound.get("host") if bound else None,
            "project_roots": [str(path) for path in projects],
            "service_file": str(service_path),
            "state_path": str(state_file),
            "heartbeat_file": str(heartbeat_path),
            "heartbeat_age_seconds": heartbeat_age_seconds(heartbeat),
            "pending_inbox_count": inbox_count,
            "deferred_event_count": len(deferred_events),
            "deferred_status_counts": deferred_counts,
            "manual_required_count": deferred_counts[DEFER_STATUS_MANUAL_REQUIRED],
            "deferred_events": deferred_events,
            "warnings": service_warnings(
                status="not_started",
                state=None,
                bound=bound,
                binding_error=binding_error,
                inbox_count=inbox_count,
                query_host=host,
                host_scoped_pending_count=host_scoped_pending_count,
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
        "host_filter": state.get("host_filter"),
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
        "deferred_event_count": len(deferred_events),
        "deferred_status_counts": deferred_counts,
        "manual_required_count": deferred_counts[DEFER_STATUS_MANUAL_REQUIRED],
        "deferred_events": deferred_events,
        "warnings": service_warnings(
            status=status,
            state=state,
            bound=bound,
            binding_error=binding_error,
            inbox_count=inbox_count,
            query_host=host,
            host_scoped_pending_count=host_scoped_pending_count,
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
    query_host: str | None = None,
    host_scoped_pending_count: int | None = None,
) -> list[str]:
    """Cross-check binding, service action and pending signals.

    A stale or mismatched delivery channel must be loud: a crashed service or
    a callback service pointed at the wrong host silently drops follow-ups,
    which the user only notices after a worker finishes without delivery.
    """
    warnings: list[str] = []
    host = bound.get("host") if bound else None
    if binding_error:
        warnings.append(f"binding is unreadable: {binding_error}")
    if (
        query_host is None
        and host in HOST_ADAPTERS
        and host_scoped_pending_count is not None
        and host_scoped_pending_count != inbox_count
    ):
        warnings.append(
            "bare service status is reading legacy watcher files, but binding "
            f"host '{host}' uses host-scoped callback state with "
            f"{host_scoped_pending_count} pending signal(s); run "
            f"`watcher --host {host} service status` for the active host channel"
        )
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
    host: str | None = None,
    replace: bool = False,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    if interval_seconds <= 0:
        raise WatcherError("interval must be positive")
    if action == "current-thread-callback" and not target_thread_id:
        raise WatcherError("target thread id is required for current-thread-callback")
    if host is not None and action != "callback":
        # Host-scoped state/service/heartbeat paths only exist for the
        # callback action; mixing --host with other actions would split the
        # service file and its heartbeat across different path schemes.
        raise WatcherError("--host is only valid with --action callback")
    if action == "callback" and host is not None and host not in HOST_ADAPTERS:
        raise WatcherError(
            f"host {host} does not support callback wakeups; "
            "use `watcher stream` from the host chat instead"
        )
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
    service_path = service_file or default_callback_service_path(
        projects[0],
        host=host,
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
    if state_path is None and action == "callback":
        watcher_state = default_callback_state_path(
            projects[0],
            host=host,
            state_dir=state_dir,
        )
    seed_state_from_legacy(
        projects[0],
        state_file=watcher_state,
        state_dir=state_dir,
    )
    heartbeat_path = default_callback_heartbeat_path(
        projects[0],
        host=host,
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
    if host is not None:
        command.extend(["--host", host])
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
        "host_filter": [host] if host else None,
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
    host: str | None = None,
    timeout_seconds: float = 5.0,
    process_checker=process_alive,
    kill_group=os.killpg,
) -> dict[str, Any]:
    projects = [path.expanduser().resolve() for path in project_roots]
    service_path = service_file or default_callback_service_path(
        projects[0],
        host=host,
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
    host_filter: set[str] | None = None,
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
                    host_filter=host_filter,
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
