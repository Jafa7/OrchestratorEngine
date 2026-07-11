"""Signal stream for hosts with native event watching (e.g. Claude sessions).

A Claude orchestrator session arms its harness watch (Monitor) on
`orchestrator-engine watcher stream`; every new inbox signal is printed as one
JSON line, which the harness turns into a chat wakeup. No push adapter needed.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import binding, core, host_capabilities, watcher


def emit_line(line: str) -> None:
    # Flush per line so a piped harness watch sees events immediately.
    print(line, flush=True)


def stream_signals(
    project_roots: list[Path],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    state_path: Path | None = None,
    interval_seconds: float = 2.0,
    emit: Callable[[str], None] = emit_line,
    max_scans: int | None = None,
    sleep=time.sleep,
    scan=watcher.scan_once,
) -> None:
    """Scan the inbox in a loop and emit one JSON line per new signal."""
    if interval_seconds <= 0:
        raise watcher.WatcherError("interval must be positive")
    projects = [path.expanduser().resolve() for path in project_roots]
    stream_state = state_path or watcher.default_stream_state_path(
        projects[0],
        host="claude",
        state_dir=state_dir,
    )
    scans = 0
    while max_scans is None or scans < max_scans:
        try:
            result = scan(
                projects,
                state_dir=state_dir,
                state_path=stream_state,
                action="record",
                host_filter={"claude"},
            )
        except (OSError, RuntimeError, ValueError) as error:
            record_stream_error(stream_state, error)
        else:
            clear_stream_error(stream_state)
            for signal in result["new_signals"]:
                emit(format_signal_line(signal))
        scans += 1
        if max_scans is not None and scans >= max_scans:
            break
        sleep(interval_seconds)


def record_stream_error(path: Path, error: BaseException) -> None:
    try:
        state = watcher.load_state(path)
    except (OSError, RuntimeError, ValueError):
        state = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": watcher.STATE_KIND,
            "seen_event_ids": [],
            "deferred_events": {},
            "acknowledged_events": {},
        }
    state["updated_at"] = core.utc_now()
    state["last_error"] = str(error)
    state["last_error_at"] = state["updated_at"]
    core.atomic_json(path, state)


def clear_stream_error(path: Path) -> None:
    try:
        state = watcher.load_state(path)
    except (OSError, RuntimeError, ValueError):
        return
    if "last_error" not in state and "last_error_at" not in state:
        return
    state.setdefault("kind", watcher.STATE_KIND)
    state.pop("last_error", None)
    state.pop("last_error_at", None)
    core.atomic_json(path, state)


def stream_status(
    project_roots: list[Path],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    state_path: Path | None = None,
    interval_seconds: float = 2.0,
) -> dict[str, Any]:
    if interval_seconds <= 0:
        raise watcher.WatcherError("interval must be positive")
    projects = [path.expanduser().resolve() for path in project_roots]
    stream_state = state_path or watcher.default_stream_state_path(
        projects[0],
        host="claude",
        state_dir=state_dir,
    )
    state = watcher.load_optional_object(stream_state)
    max_age = max(interval_seconds * 3, watcher.MIN_HEARTBEAT_MAX_AGE_SECONDS)
    age = stream_age_seconds(state)
    if state is None:
        status = "not_started"
        healthy = False
        reason = "missing"
    elif age is None:
        status = "degraded"
        healthy = False
        reason = "invalid_timestamp"
    elif age > max_age:
        status = "stale"
        healthy = False
        reason = "stale"
    elif state.get("last_error"):
        # Erroring scans refresh updated_at, so a persistently failing
        # stream would otherwise look "fresh"; the recorded error wins.
        status = "erroring"
        healthy = False
        reason = "last_error"
    else:
        status = "fresh"
        healthy = True
        reason = "fresh"
    bound = None
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        bound = binding.load_binding(projects[0], state_dir=state_dir)
    pending_count = watcher.pending_signal_count(
        projects,
        state_dir=state_dir,
        state_file=stream_state,
        host_filter={"claude"},
        fallback_binding=bound,
    )
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "LOCAL_AI_ORCHESTRATOR_STREAM_STATUS",
        "host": "claude",
        "capabilities": host_capabilities.for_host("claude"),
        "status": status,
        "healthy": healthy,
        "reason": reason,
        "project_roots": [str(path) for path in projects],
        "state_path": str(stream_state),
        "age_seconds": age,
        "max_age_seconds": max_age,
        "pending_inbox_count": pending_count,
        "last_error": state.get("last_error") if state else None,
        "last_error_at": state.get("last_error_at") if state else None,
        "checked_at": core.utc_now(),
    }


def stream_age_seconds(state: dict[str, Any] | None) -> float | None:
    if not state:
        return None
    updated_at = state.get("updated_at")
    if not isinstance(updated_at, str):
        return None
    try:
        checked = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - checked).total_seconds(), 0.0)


def format_signal_line(signal: dict[str, Any]) -> str:
    line = {
        "kind": "LOCAL_AI_ORCHESTRATOR_SIGNAL",
        "event_id": signal.get("event_id"),
        "task_id": signal.get("task_id"),
        "terminal_status": signal.get("terminal_status"),
        "event_path": signal.get("event_path"),
        "result_path": signal.get("result_path"),
        "evidence_path": signal.get("evidence_path"),
        "signal_path": signal.get("signal_path"),
        "requires": "ORCHESTRATOR_FOLLOWUP",
    }
    return json.dumps(line, ensure_ascii=False, sort_keys=True)
