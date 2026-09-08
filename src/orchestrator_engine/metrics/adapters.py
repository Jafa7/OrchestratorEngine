"""Bounded, read-only adapters for existing OrchestratorEngine artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

from orchestrator_engine import core

from . import contracts

MAX_DISCOVERY_PATHS = 100_000


def collect_orchestrator_engine(
    project_root: Path,
    *,
    source_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    maximum: int = contracts.MAX_IMPORT_RECORDS,
    cursor: str | None = None,
) -> dict[str, Any]:
    state_root = core.state_root(project_root, state_dir=state_dir)
    all_candidates = _discover_candidates(state_root)
    cursor_path = Path(cursor) if cursor else None
    remaining = [
        path for path in all_candidates if cursor_path is None or path > cursor_path
    ]
    wrapped = cursor_path is not None and not remaining
    if wrapped:
        remaining = all_candidates
    candidates = remaining[:maximum]
    more_available = len(remaining) > len(candidates)
    next_cursor = str(candidates[-1]) if candidates and more_available else None
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
        "truncated": more_available,
        "next_cursor": next_cursor,
        "wrapped": wrapped,
    }


def _discover_candidates(state_root: Path) -> list[Path]:
    patterns = (
        "tasks/*/result.json",
        "tasks/*/usage.json",
        "checks/*/verification-result.json",
        "workstreams/*/workstream.json",
        "events/*.json",
        "inbox/thread-wakeups/*.json",
        "inbox/acknowledgements/*.json",
    )
    discovered = list(
        islice(
            (path for pattern in patterns for path in state_root.glob(pattern)),
            MAX_DISCOVERY_PATHS + 1,
        )
    )
    if len(discovered) > MAX_DISCOVERY_PATHS:
        raise ValueError(
            "metrics adapter discovery exceeds its bounded 100000-path limit"
        )
    return sorted(discovered)


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
        return contracts.make_observation(
            source_id=source_id,
            record_type="work_item",
            data={
                "work_item_id": workstream_id,
                "status": value.get("status"),
                "started_at": value.get("created_at"),
                "updated_at": value.get("updated_at"),
                "waiting_on": value.get("waiting_on"),
                "evidence_pointer": str(path),
            },
            observed_at=observed_at,
            observation_id=_snapshot_id(
                source_id, "workstream", workstream_id, value
            ),
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
