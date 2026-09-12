"""Bounded, read-only adapters for existing OrchestratorEngine artifacts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator_engine import core

from . import contracts


def collect_orchestrator_engine(
    project_root: Path,
    *,
    source_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    maximum: int = contracts.MAX_IMPORT_RECORDS,
    cursor: str | None = None,
) -> dict[str, Any]:
    state_root = core.state_root(project_root, state_dir=state_dir)
    page = _candidate_page(state_root, cursor=cursor, maximum=maximum)
    candidates = page["paths"]
    loaded: list[tuple[Path, dict[str, Any]]] = []
    errors: list[dict[str, str]] = []
    for path in candidates:
        try:
            value = core.load_object(path)
            loaded.append((path, value))
        except (OSError, RuntimeError, ValueError) as error:
            errors.append({"path": str(path), "reason": str(error)[:240]})
    event_links = _event_links(loaded)
    records: list[dict[str, Any]] = []
    for path, value in loaded:
        try:
            converted = _convert(
                value,
                path=path,
                source_id=source_id,
                event_links=event_links,
                state_root=state_root,
            )
            if converted is not None:
                records.append(converted)
        except (OSError, RuntimeError, ValueError) as error:
            errors.append({"path": str(path), "reason": str(error)[:240]})
    return {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_METRICS_ADAPTER_RESULT",
        "candidate_count": len(candidates),
        "record_count": len(records),
        "error_count": len(errors),
        "errors": errors[:20],
        "records": records,
        "truncated": page["truncated"],
        "next_cursor": page["next_cursor"],
        "wrapped": False,
        "discovery": page["discovery"],
    }


PATTERNS = (
    "tasks/*/result.json",
    "tasks/*/usage.json",
    "checks/*/verification-result.json",
    "workstreams/*/workstream.json",
    "events/*.json",
    "inbox/thread-wakeups/*.json",
    "inbox/acknowledgements/*.json",
)
CURSOR_PREFIX = "metrics-index:"


def _candidate_page(
    state_root: Path,
    *,
    cursor: str | None,
    maximum: int,
) -> dict[str, Any]:
    index_path = state_root / "indexes" / "metrics-candidates.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index_path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidates(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE
            );
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        discovery = _refresh_candidate_index(connection, state_root)
        recovery_markers = discovery.pop("_recovery_markers", [])
        cursor_sequence, legacy_reset = _parse_candidate_cursor(cursor)
        maximum_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM candidates"
            ).fetchone()[0]
        )
        if cursor_sequence > maximum_sequence:
            cursor_sequence = 0
            legacy_reset = True
        valid_rows = []
        rows_examined = 0
        scan_sequence = cursor_sequence
        while len(valid_rows) <= maximum:
            rows = connection.execute(
                """SELECT sequence, path FROM candidates
                   WHERE sequence>? ORDER BY sequence LIMIT ?""",
                (scan_sequence, maximum + 1),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                scan_sequence = int(row["sequence"])
                rows_examined += 1
                path = Path(row["path"])
                if path.is_file():
                    valid_rows.append(row)
                    if len(valid_rows) > maximum:
                        break
                else:
                    connection.execute(
                        "DELETE FROM candidates WHERE sequence=?",
                        (row["sequence"],),
                    )
            if len(valid_rows) > maximum or len(rows) < maximum + 1:
                break
        truncated = len(valid_rows) > maximum
        selected = valid_rows[:maximum]
        high_water = (
            int(selected[-1]["sequence"])
            if truncated
            else max(scan_sequence, maximum_sequence)
        )
        connection.commit()
        core.clear_index_recovery_markers(recovery_markers)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "paths": [Path(row["path"]) for row in selected],
        "truncated": truncated,
        "next_cursor": f"{CURSOR_PREFIX}{high_water}",
        "discovery": {
            **discovery,
            "candidate_rows_examined": rows_examined,
            "legacy_cursor_reset": legacy_reset,
            "coverage": (
                "recovery_pending"
                if discovery["pending_recovery_markers"]
                else "through_index_sequence"
            ),
        },
    }


def _parse_candidate_cursor(cursor: str | None) -> tuple[int, bool]:
    if cursor is None:
        return 0, False
    if not cursor.startswith(CURSOR_PREFIX):
        return 0, True
    try:
        value = int(cursor.removeprefix(CURSOR_PREFIX))
    except ValueError as error:
        raise contracts.MetricsContractError(
            "invalid metrics collector cursor"
        ) from error
    if value < 0:
        raise contracts.MetricsContractError("invalid metrics collector cursor")
    return value, False


def _refresh_candidate_index(
    connection: sqlite3.Connection, state_root: Path
) -> dict[str, Any]:
    recovery = core.index_recovery_markers(
        state_root, index_name="metrics-candidates"
    )
    recoverable_markers = [*recovery["ready"], *recovery["invalid"]]
    if recoverable_markers:
        connection.execute("DELETE FROM candidates")
        connection.execute("DELETE FROM metadata")
    bootstrapped = connection.execute(
        "SELECT value FROM metadata WHERE key='bootstrapped'"
    ).fetchone()
    rebuild_examined = 0
    if bootstrapped is None:
        for pattern in PATTERNS:
            for path in state_root.glob(pattern):
                rebuild_examined += 1
                connection.execute(
                    "INSERT OR IGNORE INTO candidates(path) VALUES(?)",
                    (str(path.resolve()),),
                )
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('bootstrapped','1')"
        )
    offset_row = connection.execute(
        "SELECT value FROM metadata WHERE key='journal_offset'"
    ).fetchone()
    offset = int(offset_row["value"]) if offset_row is not None else 0
    journal = state_root / "indexes" / "metrics-candidates.jsonl"
    journal_examined = 0
    if journal.is_file():
        size = journal.stat().st_size
        if offset > size:
            offset = 0
        with journal.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    handle.seek(line_start)
                    break
                offset = handle.tell()
                journal_examined += 1
                try:
                    value = json.loads(line)
                    path = Path(value["path"]).resolve()
                    path.relative_to(state_root.resolve())
                except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not _is_candidate_path(path, state_root):
                    continue
                connection.execute("DELETE FROM candidates WHERE path=?", (str(path),))
                connection.execute(
                    "INSERT INTO candidates(path) VALUES(?)", (str(path),)
                )
    connection.execute(
        """INSERT INTO metadata(key,value) VALUES('journal_offset',?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (str(offset),),
    )
    return {
        "index_rebuilt": bootstrapped is None,
        "rebuild_paths_examined": rebuild_examined,
        "journal_records_examined": journal_examined,
        "index_path": str(state_root / "indexes" / "metrics-candidates.sqlite3"),
        "recovery_rebuilt": bool(recoverable_markers),
        "pending_recovery_markers": len(recovery["pending"]),
        "invalid_recovery_markers": len(recovery["invalid"]),
        "_recovery_markers": recoverable_markers,
    }


def _is_candidate_path(path: Path, state_root: Path) -> bool:
    try:
        relative = path.relative_to(state_root)
    except ValueError:
        return False
    parts = relative.parts
    return (
        len(parts) == 3
        and parts[0] == "tasks"
        and parts[2] in {"result.json", "usage.json"}
    ) or (
        len(parts) == 3
        and parts[0] in {"checks", "workstreams"}
        and parts[2]
        in {"verification-result.json", "workstream.json"}
    ) or (
        len(parts) == 2 and parts[0] == "events" and path.suffix == ".json"
    ) or (
        len(parts) == 3
        and parts[0] == "inbox"
        and parts[1] in {"thread-wakeups", "acknowledgements"}
        and path.suffix == ".json"
    )


def _convert(
    value: dict[str, Any],
    *,
    path: Path,
    source_id: str,
    event_links: dict[str, tuple[str, str]],
    state_root: Path,
) -> dict[str, Any] | None:
    kind = value.get("kind")
    observed_at = (
        value.get("finished_at")
        or value.get("updated_at")
        or value.get("created_at")
        or value.get("captured_at")
        or value.get("acknowledged_at")
        or datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(
            timespec="milliseconds"
        )
    )
    if kind == "WORKER_RESULT":
        data = {
            "execution_id": value.get("task_id"),
            "execution_kind": "worker",
            "observation_mode": "complement",
            "operation_id": value.get("task_id"),
            "operation_kind": "worker",
            "status": value.get("terminal_status") or value.get("status"),
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
            "duration_seconds": value.get("duration_seconds"),
            "evidence_pointer": str(path),
        }
        return contracts.make_observation(
            source_id=source_id,
            record_type="execution_attempt",
            data=data,
            observed_at=observed_at,
            observation_id=_snapshot_id(
                source_id, "worker", value.get("task_id"), value
            ),
        )
    if kind == "WORKER_USAGE":
        task_id = value.get("task_id")
        measurement_status = value.get("measurement_status", "unavailable")
        data = {
            "execution_id": task_id,
            "execution_kind": "worker",
            "observation_mode": "complement",
            "operation_id": task_id,
            "operation_kind": "worker",
            "usage_event_id": f"worker-usage:{task_id}",
            "usage_measurement_status": measurement_status,
            "evidence_pointer": str(path),
        }
        total_tokens = value.get("total_tokens")
        if measurement_status in {"complete", "partial"} and isinstance(
            total_tokens, int
        ):
            data["total_tokens"] = total_tokens
        return contracts.make_observation(
            source_id=source_id,
            record_type="execution_attempt",
            data=data,
            observed_at=observed_at,
            observation_id=_snapshot_id(source_id, "usage", task_id, value),
        )
    if kind == "ORCHESTRATOR_VERIFICATION_RESULT":
        check_id = value.get("check_id")
        data = {
            "execution_id": check_id,
            "execution_kind": "local_check",
            "operation_id": check_id,
            "operation_kind": "local_check",
            "status": "completed"
            if value.get("status") == "passed"
            else value.get("status"),
            "accepted": value.get("status") == "passed",
            "verification_role": value.get("verification_role"),
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
            "duration_seconds": value.get("duration_seconds"),
            "evidence_pointer": str(path),
        }
        return contracts.make_observation(
            source_id=source_id,
            record_type="execution_attempt",
            data=data,
            observed_at=observed_at,
            observation_id=_snapshot_id(source_id, "check", check_id, value),
        )
    if kind == "ORCHESTRATOR_WORKSTREAM":
        workstream_id = value.get("workstream_id")
        source_status = value.get("status")
        return contracts.make_observation(
            source_id=source_id,
            record_type="work_item",
            data={
                "work_item_id": workstream_id,
                "status": "completed" if source_status == "complete" else source_status,
                "source_status": source_status,
                "started_at": value.get("created_at"),
                "updated_at": value.get("updated_at"),
                "waiting_on": value.get("waiting_on"),
                "evidence_pointer": str(path),
            },
            observed_at=observed_at,
            observation_id=_snapshot_id(source_id, "workstream", workstream_id, value),
        )
    if kind in {"WORKER_TERMINAL", "ORCHESTRATOR_TERMINAL"}:
        event_id = value.get("event_id")
        operation_id, operation_kind = _terminal_operation(value)
        return contracts.make_observation(
            source_id=source_id,
            record_type="delivery",
            data={
                "delivery_id": event_id,
                "event_id": event_id,
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "status": "queued",
                "evidence_pointer": str(path),
            },
            observed_at=observed_at,
            observation_id=_snapshot_id(source_id, "event", event_id, value),
        )
    if kind in {
        "CURRENT_THREAD_WAKEUP",
        "VSCODE_CHAT_WAKEUP",
        "THREAD_WAKEUP",
        "WATCHER_CALLBACK_RECEIPT",
        "LOCAL_AI_ORCHESTRATOR_WATCHER_ACKNOWLEDGEMENT",
    }:
        event_id = value.get("event_id")
        raw_status = value.get("status")
        status = "delivered" if raw_status in {"woken", "delivered"} else raw_status
        linked_operation = _linked_operation(
            event_id,
            event_links=event_links,
            state_root=state_root,
        )
        operation_id = value.get("operation_id")
        operation_kind = value.get("source_kind")
        if linked_operation is not None:
            operation_id = operation_id or linked_operation[0]
            operation_kind = operation_kind or linked_operation[1]
        return contracts.make_observation(
            source_id=source_id,
            record_type="delivery",
            data={
                "delivery_id": event_id,
                "event_id": event_id,
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "status": status,
                "evidence_pointer": str(path),
            },
            observed_at=observed_at,
            observation_id=_snapshot_id(source_id, "delivery", event_id, value),
        )
    return None


def _event_links(
    loaded: list[tuple[Path, dict[str, Any]]],
) -> dict[str, tuple[str, str]]:
    links: dict[str, tuple[str, str]] = {}
    for _path, value in loaded:
        if value.get("kind") not in {"WORKER_TERMINAL", "ORCHESTRATOR_TERMINAL"}:
            continue
        event_id = value.get("event_id")
        operation_id, operation_kind = _terminal_operation(value)
        if (
            isinstance(event_id, str)
            and event_id
            and isinstance(operation_id, str)
            and operation_id
        ):
            links[event_id] = (operation_id, operation_kind)
    return links


def _terminal_operation(value: dict[str, Any]) -> tuple[object, str]:
    if value.get("kind") == "WORKER_TERMINAL":
        return value.get("task_id"), "worker"
    return value.get("operation_id"), str(value.get("source_kind") or "followup")


def _linked_operation(
    event_id: object,
    *,
    event_links: dict[str, tuple[str, str]],
    state_root: Path,
) -> tuple[str, str] | None:
    if not isinstance(event_id, str) or not event_id:
        return None
    if event_id in event_links:
        return event_links[event_id]
    try:
        core.validate_event_id(event_id)
        event = core.load_object(state_root / "events" / f"{event_id}.json")
    except (OSError, RuntimeError, ValueError):
        return None
    operation_id, operation_kind = _terminal_operation(event)
    if not isinstance(operation_id, str) or not operation_id:
        return None
    return operation_id, operation_kind


def _snapshot_id(
    source_id: str, category: str, native_id: object, value: dict[str, Any]
) -> str:
    digest = contracts.content_digest(
        {
            "source_id": source_id,
            "category": category,
            "native_id": native_id,
            "snapshot": value,
        }
    )
    return f"oe-{category}:{digest}"
