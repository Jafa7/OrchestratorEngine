"""Core durable file contracts for local AI orchestration."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
DEFAULT_STATE_DIR = ".orchestrator"
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "timed_out",
    "rate_limited",
    "invalid_result",
    "cancelled",
}
FOLLOWUP_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "action_required",
    "unavailable",
    "ambiguous",
}


class OrchestratorError(RuntimeError):
    """A deterministic orchestration failure."""


def is_supported_schema_version(value: object) -> bool:
    return type(value) is int and value in SUPPORTED_SCHEMA_VERSIONS


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json_text(value), encoding="utf-8")
    os.replace(temporary, path)


def claim_json(path: Path, value: object) -> bool:
    """Write `value` to `path` only if this process creates the file.

    An exclusive create is the election: concurrent writers of the same durable
    artifact — a supervisor finalizing its own task and a reaper finalizing what
    it believes is a lost one — cannot both win, so the artifact can never hold
    two divergent terminal records. The loser is told it lost and must read the
    winner's file instead of overwriting it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json_text(value))
        handle.flush()
        os.fsync(handle.fileno())
    return True


def terminal_event_id(project_root: Path, *, task_id: str) -> str:
    """Return the one event id a task's terminal event may ever use.

    The id is derived from the task rather than generated, so a repeated or
    raced emission converges on a single event file instead of leaving two
    contradictory terminal events in the durable audit trail.
    """
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"orchestrator-engine://terminal/{project_id(project_root)}/{task_id}",
        )
    )


def followup_event_id(
    project_root: Path,
    *,
    source_kind: str,
    operation_id: str,
) -> str:
    """Return the stable terminal event id for a non-worker operation."""

    identity = json.dumps(
        {
            "operation_id": operation_id,
            "project_id": project_id(project_root),
            "source_kind": source_kind,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def load_object(path: Path) -> dict[str, Any]:
    text: str | None = None
    for attempt in range(5):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except FileNotFoundError as error:
            if attempt == 4:
                raise OrchestratorError(f"file not found: {path}") from error
            time.sleep(0.02)
        except OSError as error:
            if error.errno != errno.ENODATA or attempt == 4:
                raise
            time.sleep(0.02)
    assert text is not None
    try:
        value = json.loads(text)
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
    return (
        events_root(
            project_root,
            state_dir=state_dir,
        )
        / f"{event_id}.json"
    )


def signal_path_for(
    project_root: Path,
    event_id: str,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
) -> Path:
    return (
        inbox_root(project_root, state_dir=state_dir) / "signals" / f"{event_id}.json"
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
    wake_target: dict[str, Any] | None = None,
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
    if wake_target is not None:
        event["wake_target"] = wake_target
        signal["wake_target"] = wake_target
    atomic_json(event_path, event)
    atomic_json(signal_path, signal)
    return {
        "event": event,
        "event_path": str(event_path),
        "signal_path": str(signal_path),
    }


def write_followup_event(
    project_root: Path,
    *,
    operation_id: str,
    source_kind: str,
    terminal_status: str,
    result_path: Path,
    evidence_path: Path,
    state_dir: str = DEFAULT_STATE_DIR,
    event_id: str | None = None,
    wake_target: dict[str, Any] | None = None,
    emit_signal: bool = True,
    not_before: str | None = None,
) -> dict[str, Any]:
    """Write a provider-neutral terminal event and optional follow-up signal."""

    if not operation_id:
        raise OrchestratorError("operation_id is required")
    if not source_kind:
        raise OrchestratorError("source_kind is required")
    if terminal_status not in FOLLOWUP_TERMINAL_STATUSES:
        raise OrchestratorError(f"unsupported follow-up status: {terminal_status}")
    project = project_root.expanduser().resolve()
    result = ensure_file(result_path, field="result")
    evidence = ensure_file(evidence_path, field="evidence")
    event_id = event_id or followup_event_id(
        project,
        source_kind=source_kind,
        operation_id=operation_id,
    )
    event_path = event_path_for(project, event_id, state_dir=state_dir)
    signal_path = signal_path_for(project, event_id, state_dir=state_dir)
    event = {
        "schema_version": SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_TERMINAL",
        "event_id": event_id,
        "project_id": project_id(project),
        "source_kind": source_kind,
        "operation_id": operation_id,
        "terminal_status": terminal_status,
        "result_path": str(result),
        "result_sha256": sha256_file(result),
        "evidence_path": str(evidence),
        "evidence_sha256": sha256_file(evidence),
        "created_at": utc_now(),
    }
    signal = {
        "schema_version": SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
        "event_id": event_id,
        "project_id": project_id(project),
        "source_kind": source_kind,
        "operation_id": operation_id,
        "event_path": str(event_path),
        "terminal_status": terminal_status,
        "result_path": str(result),
        "evidence_path": str(evidence),
        "created_at": event["created_at"],
        "requires": "ORCHESTRATOR_FOLLOWUP",
    }
    if wake_target is not None:
        event["wake_target"] = wake_target
        signal["wake_target"] = wake_target
    if not_before is not None:
        try:
            parsed = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
        except ValueError as error:
            raise OrchestratorError(
                "not_before must be an ISO-8601 timestamp"
            ) from error
        if parsed.tzinfo is None:
            raise OrchestratorError("not_before must include a timezone")
        normalized = parsed.astimezone(UTC).isoformat(timespec="milliseconds")
        event["not_before"] = normalized
        signal["not_before"] = normalized
    atomic_json(event_path, event)
    if emit_signal:
        atomic_json(signal_path, signal)
    return {
        "event": event,
        "event_path": str(event_path),
        "signal_path": str(signal_path) if emit_signal else None,
        "signal_emitted": emit_signal,
    }


def verify_terminal_event(event_path: Path) -> dict[str, Any]:
    event = load_object(event_path.expanduser().resolve())
    if not is_supported_schema_version(event.get("schema_version")):
        raise OrchestratorError("unsupported terminal event schema")
    kind = event.get("kind")
    if kind not in {"WORKER_TERMINAL", "ORCHESTRATOR_TERMINAL"}:
        raise OrchestratorError("unsupported terminal event kind")
    identity_keys = (
        ("event_id", "project_id", "task_id")
        if kind == "WORKER_TERMINAL"
        else ("event_id", "project_id", "source_kind", "operation_id")
    )
    for key in identity_keys:
        if not isinstance(event.get(key), str) or not event[key]:
            raise OrchestratorError(f"terminal event has invalid {key}")
    allowed_statuses = (
        TERMINAL_STATUSES
        if kind == "WORKER_TERMINAL"
        else FOLLOWUP_TERMINAL_STATUSES
    )
    if event.get("terminal_status") not in allowed_statuses:
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


def subject_fields(value: dict[str, Any]) -> dict[str, str]:
    """Return the public subject identity shared by events, signals and receipts."""

    task_id = value.get("task_id")
    if isinstance(task_id, str) and task_id:
        return {"task_id": task_id}
    operation_id = value.get("operation_id")
    source_kind = value.get("source_kind")
    if isinstance(operation_id, str) and operation_id:
        fields = {"operation_id": operation_id}
        if isinstance(source_kind, str) and source_kind:
            fields["source_kind"] = source_kind
        return fields
    raise OrchestratorError("terminal artifact has no valid subject identity")


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


def survey_schema_versions(
    project_root: Path,
    *,
    state_dir: str = DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Read durable orchestrator JSON files and bucket schema versions."""

    project = project_root.expanduser().resolve()
    candidates: list[Path] = []
    candidates.extend(sorted(events_root(project, state_dir=state_dir).glob("*.json")))
    candidates.extend(
        sorted(inbox_root(project, state_dir=state_dir).glob("**/*.json"))
    )
    candidates.extend(
        sorted((state_root(project, state_dir=state_dir) / "tasks").glob("*/*.json"))
    )
    candidates.extend(
        sorted(
            (state_root(project, state_dir=state_dir) / "monitors").glob("*/*/*.json")
        )
    )
    candidates.extend(
        sorted(
            (state_root(project, state_dir=state_dir) / "checks").glob("*/*.json")
        )
    )
    history = state_root(project, state_dir=state_dir) / "check-history.json"
    if history.is_file():
        candidates.append(history)
    candidates.extend(
        sorted(
            (state_root(project, state_dir=state_dir) / "workstreams").glob(
                "*/*.json"
            )
        )
    )
    candidates.extend(
        sorted(
            (state_root(project, state_dir=state_dir) / "workstreams").glob(
                "*/checkpoints/*.json"
            )
        )
    )
    supported: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    for path in candidates:
        if path.is_symlink():
            unreadable.append(
                {"path": str(path), "error": "durable artifact must not be a symlink"}
            )
            continue
        try:
            value = load_object(path)
        except (OSError, OrchestratorError) as error:
            unreadable.append({"path": str(path), "error": str(error)})
            continue
        version = value.get("schema_version")
        row = {
            "path": str(path),
            "kind": value.get("kind"),
            "schema_version": version,
        }
        if is_supported_schema_version(version):
            supported.append(row)
        else:
            unsupported.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_SCHEMA_SURVEY",
        "project_root": str(project),
        "state_dir": state_dir,
        "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
        "supported_count": len(supported),
        "unsupported_count": len(unsupported),
        "unreadable_count": len(unreadable),
        "supported": supported,
        "unsupported": unsupported,
        "unreadable": unreadable,
        "checked_at": utc_now(),
    }


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
