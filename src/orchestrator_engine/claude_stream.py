"""Signal stream for hosts with native event watching (e.g. Claude sessions).

A Claude orchestrator session arms its harness watch (Monitor) on
`orchestrator-engine watcher stream`; every new inbox signal is printed as one
JSON line, which the harness turns into a chat wakeup. No push adapter needed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import core, watcher


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
        result = watcher.scan_once(
            projects,
            state_dir=state_dir,
            state_path=stream_state,
            action="record",
            host_filter={"claude"},
        )
        for signal in result["new_signals"]:
            emit(format_signal_line(signal))
        scans += 1
        if max_scans is not None and scans >= max_scans:
            break
        sleep(interval_seconds)


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
