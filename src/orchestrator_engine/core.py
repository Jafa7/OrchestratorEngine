"""Core durable file contracts for local AI orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_STATE_DIR = ".orchestrator"
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "timed_out",
    "rate_limited",
    "invalid_result",
    "cancelled",
}


class OrchestratorError(RuntimeError):
    """A deterministic orchestration failure."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise OrchestratorError(f"file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise OrchestratorError(f"invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise OrchestratorError(f"JSON value must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_root(
    project_root: Path,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
) -> Path:
    return project_root.expanduser().resolve() / state_dir


def events_root(
    project_root: Path,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
) -> Path:
    return state_root(project_root, state_dir=state_dir) / "events"


def inbox_root(
    project_root: Path,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
) -> Path:
    return state_root(project_root, state_dir=state_dir) / "inbox"


def event_path_for(
    project_root: Path,
    event_id: str,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
) -> Path:
    return events_root(
        project_root,
        state_dir=state_dir,
    ) / f"{event_id}.json"


def signal_path_for(
    project_root: Path,
    event_id: str,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
) -> Path:
    return (
        inbox_root(project_root, state_dir=state_dir)
        / "signals"
        / f"{event_id}.json"
    )


def project_id(project_root: Path) -> str:
    return project_root.expanduser().resolve().name


def ensure_file(path: Path, *, field: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise OrchestratorError(f"{field} is not a file: {resolved}")
    return resolved


def write_terminal_event(
    project_root: Path,
    *,
    task_id: str,
    terminal_status: str,
    result_path: Path,
    evidence_path: Path,
    state_dir: str = DEFAULT_STATE_DIR,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Write a terminal event and matching orchestrator inbox signal."""

    if not task_id:
        raise OrchestratorError("task_id is required")
    if terminal_status not in TERMINAL_STATUSES:
        raise OrchestratorError(f"unsupported terminal status: {terminal_status}")
    project = project_root.expanduser().resolve()
    result = ensure_file(result_path, field="result")
    evidence = ensure_file(evidence_path, field="evidence")
    event_id = event_id or str(uuid.uuid4())
    event_path = event_path_for(
        project,
        event_id,
        state_dir=state_dir,
    )
    signal_path = signal_path_for(
        project,
        event_id,
        state_dir=state_dir,
    )
    event = {
        "schema_version": SCHEMA_VERSION,
        "kind": "WORKER_TERMINAL",
        "event_id": event_id,
        "project_id": project_id(project),
        "task_id": task_id,
        "terminal_status": terminal_status,
        "result_path": str(result),
        "result_sha256": sha256_file(result),
        "evidence_path": str(evidence),
        "evidence_sha256": sha256_file(evidence),
        "created_at": utc_now(),
    }
    signal = {
        "schema_version": SCHEMA_VERSION,
        "kind": "LOCAL_AI_WORKER_FINISHED",
        "event_id": event_id,
        "project_id": project_id(project),
        "task_id": task_id,
        "event_path": str(event_path),
        "terminal_status": terminal_status,
        "result_path": str(result),
        "evidence_path": str(evidence),
        "created_at": event["created_at"],
        "requires": "ORCHESTRATOR_REVIEW",
    }
    atomic_json(event_path, event)
    atomic_json(signal_path, signal)
    return {
        "event": event,
        "event_path": str(event_path),
        "signal_path": str(signal_path),
    }


def verify_terminal_event(event_path: Path) -> dict[str, Any]:
    event = load_object(event_path.expanduser().resolve())
    if event.get("schema_version") != SCHEMA_VERSION:
        raise OrchestratorError("unsupported terminal event schema")
    if event.get("kind") != "WORKER_TERMINAL":
        raise OrchestratorError("unsupported terminal event kind")
    for key in ("event_id", "project_id", "task_id"):
        if not isinstance(event.get(key), str) or not event[key]:
            raise OrchestratorError(f"terminal event has invalid {key}")
    if event.get("terminal_status") not in TERMINAL_STATUSES:
        raise OrchestratorError("terminal event has invalid terminal_status")
    for path_key, hash_key in (
        ("result_path", "result_sha256"),
        ("evidence_path", "evidence_sha256"),
    ):
        path_value = event.get(path_key)
        expected = event.get(hash_key)
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise OrchestratorError(f"terminal event is missing {path_key}/{hash_key}")
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            raise OrchestratorError(f"terminal artifact is unavailable: {path}")
        if sha256_file(path) != expected:
            raise OrchestratorError(f"terminal artifact hash mismatch: {path}")
    return event


def inbox(
    project_root: Path,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
    invalid_sink: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """List inbox signals.

    With `invalid_sink`, unreadable signal files (e.g. written non-atomically
    by a project-side supervisor) are reported there and skipped instead of
    failing the whole listing — a long-running watcher must survive them.
    """
    signals = inbox_root(project_root, state_dir=state_dir) / "signals"
    rows: list[dict[str, Any]] = []
    for path in sorted(signals.glob("*.json")):
        try:
            signal = load_object(path)
        except (OSError, OrchestratorError) as error:
            if invalid_sink is None:
                raise
            invalid_sink.append({"signal_path": str(path), "error": str(error)})
            continue
        signal["signal_path"] = str(path)
        rows.append(signal)
    return rows


def compact_line_log(path: Path, *, keep_bytes: int) -> None:
    with path.open("a+b") as handle:
        handle.seek(0)
        rows = handle.read().splitlines(keepends=True)
        kept: list[bytes] = []
        size = 0
        for row in reversed(rows):
            if kept and size + len(row) > keep_bytes:
                break
            kept.append(row)
            size += len(row)
        handle.seek(0)
        handle.truncate()
        handle.write(b"".join(reversed(kept)))
        handle.flush()
        os.fsync(handle.fileno())


def cleanup(
    project_root: Path,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
    retention_days: int = 30,
    log_max_bytes: int = 50 * 1024 * 1024,
    log_keep_bytes: int = 10 * 1024 * 1024,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Prune old local orchestration artifacts with bounded log retention."""

    if retention_days < 1:
        raise OrchestratorError("retention_days must be positive")
    if log_keep_bytes < 1 or log_max_bytes <= log_keep_bytes:
        raise OrchestratorError("log retention sizes are invalid")
    current = now or datetime.now(UTC)
    cutoff = (current - timedelta(days=retention_days)).timestamp()
    root = inbox_root(project_root, state_dir=state_dir)
    removed: list[str] = []
    compacted: list[str] = []

    def old(path: Path) -> bool:
        return path.is_file() and path.stat().st_mtime <= cutoff

    def remove(path: Path) -> None:
        if not path.is_file():
            return
        removed.append(str(path))
        if not dry_run:
            path.unlink()

    for directory_name in ("notifications", "thread-wakeups"):
        for path in sorted((root / directory_name).glob("*.json")):
            if old(path):
                remove(path)
    for path in sorted((root / "logs").glob("*.log")):
        if old(path) and path.name != "watcher-service.log":
            remove(path)
    for path in (root / "logs" / "watcher-service.log",):
        if path.is_file() and path.stat().st_size > log_max_bytes:
            compacted.append(str(path))
            if not dry_run:
                compact_line_log(path, keep_bytes=log_keep_bytes)
    return {
        "schema_version": SCHEMA_VERSION,
        "dry_run": dry_run,
        "removed_count": len(removed),
        "removed": removed,
        "compacted": compacted,
        "policy": {
            "retention_days": retention_days,
            "log_max_bytes": log_max_bytes,
            "log_keep_bytes": log_keep_bytes,
        },
    }
