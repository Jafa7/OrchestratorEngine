"""Durable, bounded checkpoints for explicitly continued agent workstreams."""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import binding, core, platform_runtime

WORKSTREAM_KIND = "ORCHESTRATOR_WORKSTREAM"
CHECKPOINT_KIND = "ORCHESTRATOR_WORKSTREAM_CHECKPOINT"
SOURCE_KIND = "workstream_checkpoint"
DECISIONS = {
    "continue",
    "waiting_external",
    "needs_user",
    "blocked",
    "complete",
    "paused",
}
AUTOMATIC_DECISIONS = {"continue", "waiting_external"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DEFAULT_DELAY_SECONDS = 10.0
DEFAULT_MAX_CONTINUATIONS = 8
DEFAULT_MAX_WALL_SECONDS = 4 * 60 * 60
MAX_TEXT_LENGTH = 4000
MAX_DELAY_SECONDS = 60 * 60
MAX_CONTINUATIONS = 100
MAX_WALL_SECONDS = 7 * 24 * 60 * 60


class WorkstreamError(RuntimeError):
    """A deterministic workstream contract failure."""


def validate_id(value: str, *, field: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise WorkstreamError(f"invalid {field}: {value!r}")
    return value


def bounded_text(value: str, *, field: str, required: bool = True) -> str:
    text = value.strip()
    if required and not text:
        raise WorkstreamError(f"{field} is required")
    if len(text) > MAX_TEXT_LENGTH:
        raise WorkstreamError(f"{field} exceeds {MAX_TEXT_LENGTH} characters")
    return text


def positive_limit(value: int, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or value <= 0 or value > maximum:
        raise WorkstreamError(f"{field} must be between 1 and {maximum}")
    return value


def workstreams_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "workstreams"


def workstream_dir(
    project_root: Path,
    workstream_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return workstreams_root(project_root, state_dir=state_dir) / validate_id(
        workstream_id, field="workstream id"
    )


def descriptor_path(
    project_root: Path,
    workstream_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return workstream_dir(
        project_root, workstream_id, state_dir=state_dir
    ) / "workstream.json"


def checkpoint_path(
    project_root: Path,
    workstream_id: str,
    checkpoint_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return (
        workstream_dir(project_root, workstream_id, state_dir=state_dir)
        / "checkpoints"
        / f"{validate_id(checkpoint_id, field='checkpoint id')}.json"
    )


@contextlib.contextmanager
def workstream_lock(project_root: Path, workstream_id: str, *, state_dir: str):
    path = workstream_dir(project_root, workstream_id, state_dir=state_dir) / ".lock"
    with platform_runtime.exclusive_file_lock(path):
        yield


def capture_wake_target(project_root: Path, *, state_dir: str) -> dict[str, Any]:
    bound = binding.require_binding(project_root, state_dir=state_dir)
    return binding.wake_target_from_binding(bound)


def start_workstream(
    project_root: Path,
    *,
    workstream_id: str,
    goal: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    max_continuations: int = DEFAULT_MAX_CONTINUATIONS,
    max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    workstream_id = validate_id(workstream_id, field="workstream id")
    goal = bounded_text(goal, field="goal")
    if delay_seconds < 0 or delay_seconds > MAX_DELAY_SECONDS:
        raise WorkstreamError(
            f"delay_seconds must be between 0 and {MAX_DELAY_SECONDS}"
        )
    max_continuations = positive_limit(
        max_continuations,
        field="max_continuations",
        maximum=MAX_CONTINUATIONS,
    )
    max_wall_seconds = positive_limit(
        max_wall_seconds,
        field="max_wall_seconds",
        maximum=MAX_WALL_SECONDS,
    )
    created_at = core.utc_now()
    descriptor: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": WORKSTREAM_KIND,
        "workstream_id": workstream_id,
        "project_id": core.project_id(project),
        "goal": goal,
        "status": "active",
        "checkpoint_count": 0,
        "continuation_count": 0,
        "max_continuations": max_continuations,
        "delay_seconds": round(float(delay_seconds), 3),
        "max_wall_seconds": max_wall_seconds,
        "created_at": created_at,
        "updated_at": created_at,
    }
    wake_target = capture_wake_target(project, state_dir=state_dir)
    if wake_target is not None:
        descriptor["wake_target"] = wake_target
    path = descriptor_path(project, workstream_id, state_dir=state_dir)
    if not core.claim_json(path, descriptor):
        raise WorkstreamError(f"workstream already exists: {workstream_id}")
    return {**descriptor, "workstream_path": str(path)}


def load_workstream(
    project_root: Path,
    workstream_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    path = descriptor_path(project_root, workstream_id, state_dir=state_dir)
    value = core.load_object(path)
    validate_workstream(value, path=path, workstream_id=workstream_id)
    return value


def validate_workstream(
    value: dict[str, Any],
    *,
    path: Path,
    workstream_id: str | None = None,
) -> None:
    if (
        not core.is_supported_schema_version(value.get("schema_version"))
        or value.get("kind") != WORKSTREAM_KIND
        or not isinstance(value.get("workstream_id"), str)
        or (workstream_id is not None and value.get("workstream_id") != workstream_id)
    ):
        raise WorkstreamError(f"invalid workstream descriptor: {path}")


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WorkstreamError(f"invalid workstream timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise WorkstreamError(f"workstream timestamp has no timezone: {value}")
    return parsed.astimezone(UTC)


def limit_reason(descriptor: dict[str, Any], *, now: datetime) -> str | None:
    if int(descriptor.get("continuation_count", 0)) >= int(
        descriptor["max_continuations"]
    ):
        return "maximum automatic continuations reached"
    created = parse_timestamp(str(descriptor["created_at"]))
    if (now - created).total_seconds() >= int(descriptor["max_wall_seconds"]):
        return "maximum automatic continuation wall time reached"
    return None


def wall_limit_reason(descriptor: dict[str, Any], *, now: datetime) -> str | None:
    created = parse_timestamp(str(descriptor["created_at"]))
    if (now - created).total_seconds() >= int(descriptor["max_wall_seconds"]):
        return "maximum automatic continuation wall time reached"
    return None


def continuation_operation_id(workstream_id: str, checkpoint_id: str) -> str:
    """Encode a workstream/checkpoint pair without delimiter ambiguity."""

    return f"workstream:{workstream_id}:{checkpoint_id}"


def parse_continuation_operation_id(operation_id: str) -> tuple[str, str] | None:
    if not operation_id.startswith("workstream:"):
        return None
    try:
        workstream_id, checkpoint_id = operation_id.removeprefix(
            "workstream:"
        ).split(":", maxsplit=1)
        return (
            validate_id(workstream_id, field="workstream id"),
            validate_id(checkpoint_id, field="checkpoint id"),
        )
    except (ValueError, WorkstreamError):
        return None


def validate_checkpoint(
    value: dict[str, Any],
    *,
    path: Path,
    workstream_id: str,
) -> None:
    if (
        not core.is_supported_schema_version(value.get("schema_version"))
        or value.get("kind") != CHECKPOINT_KIND
        or value.get("workstream_id") != workstream_id
        or not isinstance(value.get("checkpoint_id"), str)
        or not isinstance(value.get("sequence"), int)
        or value.get("sequence", 0) < 1
        or value.get("decision") not in DECISIONS
    ):
        raise WorkstreamError(f"invalid workstream checkpoint: {path}")


def _checkpoint_artifact_paths(path: Path) -> tuple[Path, Path]:
    checkpoint_id = path.stem
    return (
        path.with_name(f"{checkpoint_id}.result.json"),
        path.with_name(f"{checkpoint_id}.evidence.json"),
    )


def _write_checkpoint_artifacts(path: Path, checkpoint: dict[str, Any]) -> None:
    result_path, evidence_path = _checkpoint_artifact_paths(path)
    core.atomic_json(
        result_path,
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "ORCHESTRATOR_WORKSTREAM_CONTINUATION",
            "workstream_id": checkpoint["workstream_id"],
            "checkpoint_id": checkpoint["checkpoint_id"],
            "status": "ready",
            "summary": checkpoint["summary"],
            "next_action": checkpoint["next_action"],
            "not_before": checkpoint["not_before"],
        },
    )
    core.atomic_json(
        evidence_path,
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "ORCHESTRATOR_WORKSTREAM_EVIDENCE",
            "workstream_id": checkpoint["workstream_id"],
            "checkpoint_id": checkpoint["checkpoint_id"],
            "ready_declared": True,
            "checkpoint_path": str(path),
            "created_at": checkpoint["created_at"],
        },
    )


def _apply_checkpoint(
    project: Path,
    descriptor: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    state_dir: str,
) -> bool:
    sequence = int(checkpoint["sequence"])
    applied = int(descriptor.get("checkpoint_count", 0))
    if sequence <= applied:
        return False
    if sequence != applied + 1:
        raise WorkstreamError(
            f"checkpoint sequence gap for {descriptor['workstream_id']}: "
            f"expected {applied + 1}, found {sequence}"
        )

    decision = str(checkpoint["decision"])
    descriptor.update(
        status="active" if decision == "continue" else decision,
        checkpoint_count=sequence,
        latest_checkpoint_id=checkpoint["checkpoint_id"],
        latest_checkpoint_path=str(
            checkpoint_path(
                project,
                str(checkpoint["workstream_id"]),
                str(checkpoint["checkpoint_id"]),
                state_dir=state_dir,
            )
        ),
        updated_at=checkpoint["created_at"],
    )
    if decision in AUTOMATIC_DECISIONS:
        descriptor["continuation_count"] = int(
            descriptor.get("continuation_count", 0)
        ) + 1
    if decision == "continue":
        descriptor["active_continuation"] = {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "sequence": sequence,
            "event_id": checkpoint["event_id"],
            "not_before": checkpoint["not_before"],
        }
        descriptor["last_scheduled_event_id"] = checkpoint["event_id"]
        descriptor["last_scheduled_wake_at"] = checkpoint["not_before"]
    else:
        descriptor.pop("active_continuation", None)
    if decision == "waiting_external":
        descriptor["waiting_on"] = checkpoint["waiting_on"]
    else:
        descriptor.pop("waiting_on", None)
    core.atomic_json(
        descriptor_path(
            project, str(descriptor["workstream_id"]), state_dir=state_dir
        ),
        descriptor,
    )
    return True


def _ensure_checkpoint_event(
    project: Path,
    checkpoint: dict[str, Any],
    *,
    path: Path,
    state_dir: str,
) -> dict[str, Any] | None:
    if checkpoint.get("decision") != "continue":
        return None
    _write_checkpoint_artifacts(path, checkpoint)
    result_path, evidence_path = _checkpoint_artifact_paths(path)
    wake_target = checkpoint.get("wake_target")
    operation_id = checkpoint.get("operation_id")
    if not isinstance(operation_id, str):
        operation_id = (
            f"{checkpoint['workstream_id']}.{checkpoint['checkpoint_id']}"
        )
    return core.write_followup_event(
        project,
        operation_id=operation_id,
        source_kind=SOURCE_KIND,
        terminal_status="completed",
        result_path=result_path,
        evidence_path=evidence_path,
        state_dir=state_dir,
        event_id=str(checkpoint["event_id"]),
        wake_target=wake_target if isinstance(wake_target, dict) else None,
        not_before=str(checkpoint["not_before"]),
    )


def _checkpoint_files(project: Path, workstream_id: str, *, state_dir: str):
    directory = (
        workstream_dir(project, workstream_id, state_dir=state_dir) / "checkpoints"
    )
    for path in directory.glob("*.json"):
        if path.name.endswith((".result.json", ".evidence.json")):
            continue
        yield path


def _reconcile_locked(
    project: Path,
    workstream_id: str,
    *,
    state_dir: str,
    now: datetime,
) -> dict[str, Any]:
    descriptor = load_workstream(project, workstream_id, state_dir=state_dir)
    checkpoints: list[tuple[Path, dict[str, Any]]] = []
    for path in _checkpoint_files(project, workstream_id, state_dir=state_dir):
        checkpoint = core.load_object(path)
        validate_checkpoint(checkpoint, path=path, workstream_id=workstream_id)
        checkpoints.append((path, checkpoint))
    checkpoints.sort(key=lambda item: int(item[1]["sequence"]))
    sequences: set[int] = set()
    recovered_events = 0
    applied_checkpoints = 0
    for path, checkpoint in checkpoints:
        sequence = int(checkpoint["sequence"])
        if sequence in sequences:
            raise WorkstreamError(
                f"duplicate checkpoint sequence {sequence} for {workstream_id}"
            )
        sequences.add(sequence)
        if _apply_checkpoint(
            project, descriptor, checkpoint, state_dir=state_dir
        ):
            applied_checkpoints += 1
        if checkpoint.get("decision") == "continue":
            signal_path = Path(str(checkpoint["signal_path"]))
            if not signal_path.is_file():
                _ensure_checkpoint_event(
                    project, checkpoint, path=path, state_dir=state_dir
                )
                recovered_events += 1

    if descriptor.get("status") in {"active", "waiting_external"}:
        reason = wall_limit_reason(descriptor, now=now)
        if reason is not None:
            descriptor["status"] = "needs_user"
            descriptor["reason"] = reason
            descriptor["updated_at"] = now.isoformat(timespec="milliseconds")
            descriptor.pop("active_continuation", None)
            descriptor.pop("waiting_on", None)
            core.atomic_json(
                descriptor_path(project, workstream_id, state_dir=state_dir),
                descriptor,
            )
    return {
        "workstream_id": workstream_id,
        "applied_checkpoints": applied_checkpoints,
        "recovered_events": recovered_events,
        "status": descriptor["status"],
    }


def reconcile_workstreams(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Recover interrupted checkpoint transitions without a model turn."""

    project = project_root.expanduser().resolve()
    current = now or datetime.now(UTC)
    reconciled: list[dict[str, Any]] = []
    root = workstreams_root(project, state_dir=state_dir)
    for path in sorted(root.glob("*/workstream.json")):
        workstream_id = path.parent.name
        with workstream_lock(project, workstream_id, state_dir=state_dir):
            reconciled.append(
                _reconcile_locked(
                    project, workstream_id, state_dir=state_dir, now=current
                )
            )
    return reconciled


@contextlib.contextmanager
def continuation_delivery_guard(
    project_root: Path,
    signal: dict[str, Any],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    now: datetime | None = None,
):
    """Serialize a continuation delivery with stop/resume state changes."""

    if signal.get("source_kind") != SOURCE_KIND:
        yield {"deliver": True}
        return
    operation_id = signal.get("operation_id")
    identity = (
        parse_continuation_operation_id(operation_id)
        if isinstance(operation_id, str)
        else None
    )
    if identity is None:
        result_path = signal.get("result_path")
        try:
            result = core.load_object(Path(str(result_path)))
            identity = (
                validate_id(str(result["workstream_id"]), field="workstream id"),
                validate_id(str(result["checkpoint_id"]), field="checkpoint id"),
            )
        except (KeyError, OSError, RuntimeError, ValueError):
            yield {"deliver": False, "reason": "invalid_workstream_identity"}
            return
    project = project_root.expanduser().resolve()
    workstream_id, checkpoint_id = identity
    with workstream_lock(project, workstream_id, state_dir=state_dir):
        try:
            descriptor = load_workstream(
                project, workstream_id, state_dir=state_dir
            )
            path = checkpoint_path(
                project, workstream_id, checkpoint_id, state_dir=state_dir
            )
            checkpoint = core.load_object(path)
            validate_checkpoint(
                checkpoint, path=path, workstream_id=workstream_id
            )
        except (OSError, RuntimeError, ValueError):
            yield {"deliver": False, "reason": "invalid_workstream_state"}
            return
        active = descriptor.get("active_continuation")
        expected_event = (
            active.get("event_id") if isinstance(active, dict) else None
        )
        if expected_event is None:
            expected_event = descriptor.get("last_scheduled_event_id")
        reason = wall_limit_reason(descriptor, now=now or datetime.now(UTC))
        if reason is not None:
            descriptor["status"] = "needs_user"
            descriptor["reason"] = reason
            descriptor["updated_at"] = core.utc_now()
            descriptor.pop("active_continuation", None)
            core.atomic_json(
                descriptor_path(project, workstream_id, state_dir=state_dir),
                descriptor,
            )
            yield {"deliver": False, "reason": "workstream_expired"}
            return
        if (
            descriptor.get("status") != "active"
            or checkpoint.get("decision") != "continue"
            or checkpoint.get("event_id") != signal.get("event_id")
            or expected_event != signal.get("event_id")
        ):
            yield {"deliver": False, "reason": "workstream_continuation_revoked"}
            return
        yield {
            "deliver": True,
            "workstream_id": workstream_id,
            "checkpoint_id": checkpoint_id,
        }


def checkpoint_workstream(
    project_root: Path,
    *,
    workstream_id: str,
    checkpoint_id: str,
    decision: str,
    summary: str,
    next_action: str | None = None,
    waiting_on: str | None = None,
    ready: bool = False,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    workstream_id = validate_id(workstream_id, field="workstream id")
    checkpoint_id = validate_id(checkpoint_id, field="checkpoint id")
    if decision not in DECISIONS:
        raise WorkstreamError(f"unsupported checkpoint decision: {decision}")
    summary = bounded_text(summary, field="summary")
    normalized_next = (
        bounded_text(next_action, field="next_action")
        if next_action is not None
        else None
    )
    normalized_waiting_on = (
        bounded_text(waiting_on, field="waiting_on")
        if waiting_on is not None
        else None
    )
    if decision == "continue" and not ready:
        raise WorkstreamError("continue requires explicit --ready")
    if decision == "continue" and normalized_next is None:
        raise WorkstreamError("continue requires --next-action")
    if decision != "continue" and ready:
        raise WorkstreamError("--ready is only valid with decision continue")
    if decision == "waiting_external" and normalized_waiting_on is None:
        raise WorkstreamError("waiting_external requires --waiting-on")
    if decision != "waiting_external" and normalized_waiting_on is not None:
        raise WorkstreamError("--waiting-on is only valid with waiting_external")

    path = checkpoint_path(
        project, workstream_id, checkpoint_id, state_dir=state_dir
    )
    with workstream_lock(project, workstream_id, state_dir=state_dir):
        if path.is_file():
            existing = core.load_object(path)
            validate_checkpoint(existing, path=path, workstream_id=workstream_id)
            expected = (decision, summary, normalized_next, normalized_waiting_on)
            actual = (
                existing.get("requested_decision", existing.get("decision")),
                existing.get("summary"),
                existing.get("next_action"),
                existing.get("waiting_on"),
            )
            if actual != expected:
                raise WorkstreamError(
                    "checkpoint id already exists with different content: "
                    f"{checkpoint_id}"
                )
            descriptor = load_workstream(
                project, workstream_id, state_dir=state_dir
            )
            _apply_checkpoint(
                project, descriptor, existing, state_dir=state_dir
            )
            output = {**existing, "checkpoint_path": str(path), "idempotent": True}
            signal_path = existing.get("signal_path")
            if (
                existing.get("decision") == "continue"
                and isinstance(signal_path, str)
                and not Path(signal_path).is_file()
            ):
                output["followup"] = _ensure_checkpoint_event(
                    project, existing, path=path, state_dir=state_dir
                )
                output["recovered_signal"] = True
            return output

        descriptor = load_workstream(project, workstream_id, state_dir=state_dir)
        if descriptor.get("status") == "complete":
            raise WorkstreamError("completed workstream cannot accept checkpoints")
        if decision == "continue" and descriptor.get("status") != "active":
            raise WorkstreamError(
                f"workstream is {descriptor.get('status')}; resume it before continue"
            )
        now = datetime.now(UTC)
        effective_decision = decision
        reason: str | None = None
        automatic_resume = decision in AUTOMATIC_DECISIONS
        emit_signal = decision == "continue"
        if automatic_resume:
            reason = limit_reason(descriptor, now=now)
            if reason is not None:
                effective_decision = "needs_user"
                emit_signal = False

        sequence = int(descriptor.get("checkpoint_count", 0)) + 1
        created_at = now.isoformat(timespec="milliseconds")
        checkpoint: dict[str, Any] = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": CHECKPOINT_KIND,
            "workstream_id": workstream_id,
            "checkpoint_id": checkpoint_id,
            "sequence": sequence,
            "decision": effective_decision,
            "requested_decision": decision,
            "summary": summary,
            "created_at": created_at,
        }
        if normalized_next is not None:
            checkpoint["next_action"] = normalized_next
        if normalized_waiting_on is not None:
            checkpoint["waiting_on"] = normalized_waiting_on
        if reason is not None:
            checkpoint["reason"] = reason
        wake_target = descriptor.get("wake_target")
        if isinstance(wake_target, dict):
            checkpoint["wake_target"] = wake_target

        event_result: dict[str, Any] | None = None
        if emit_signal:
            due_at = now + timedelta(seconds=float(descriptor["delay_seconds"]))
            checkpoint["not_before"] = due_at.isoformat(timespec="milliseconds")
            checkpoint["operation_id"] = continuation_operation_id(
                workstream_id, checkpoint_id
            )
            checkpoint["event_id"] = core.followup_event_id(
                project,
                source_kind=SOURCE_KIND,
                operation_id=checkpoint["operation_id"],
            )
            checkpoint["event_path"] = str(
                core.event_path_for(
                    project, checkpoint["event_id"], state_dir=state_dir
                )
            )
            checkpoint["signal_path"] = str(
                core.signal_path_for(
                    project, checkpoint["event_id"], state_dir=state_dir
                )
            )

        core.atomic_json(path, checkpoint)
        _apply_checkpoint(
            project, descriptor, checkpoint, state_dir=state_dir
        )
        if emit_signal:
            event_result = _ensure_checkpoint_event(
                project, checkpoint, path=path, state_dir=state_dir
            )

    output = {**checkpoint, "checkpoint_path": str(path), "idempotent": False}
    if event_result is not None:
        output["followup"] = event_result
    return output


def resume_workstream(
    project_root: Path,
    *,
    workstream_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    with workstream_lock(project, workstream_id, state_dir=state_dir):
        descriptor = load_workstream(project, workstream_id, state_dir=state_dir)
        if descriptor.get("status") == "complete":
            raise WorkstreamError("completed workstream cannot be resumed")
        if descriptor.get("status") == "active":
            raise WorkstreamError("active workstream does not need resume")
        descriptor["status"] = "active"
        descriptor["updated_at"] = core.utc_now()
        descriptor.pop("waiting_on", None)
        descriptor.pop("active_continuation", None)
        descriptor.pop("reason", None)
        core.atomic_json(
            descriptor_path(project, workstream_id, state_dir=state_dir), descriptor
        )
    return {**descriptor, "workstream_path": str(
        descriptor_path(project, workstream_id, state_dir=state_dir)
    )}


def workstream_status(
    project_root: Path,
    *,
    workstream_id: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    if workstream_id is not None:
        streams = [load_workstream(project, workstream_id, state_dir=state_dir)]
    else:
        streams = []
        invalid: list[dict[str, str]] = []
        pattern = workstreams_root(project, state_dir=state_dir).glob(
            "*/workstream.json"
        )
        for path in sorted(pattern):
            try:
                value = core.load_object(path)
                validate_workstream(value, path=path)
                streams.append(value)
            except (OSError, RuntimeError, ValueError) as error:
                invalid.append({"path": str(path), "error": str(error)})
    if workstream_id is not None:
        invalid = []
    counts: dict[str, int] = {}
    for stream in streams:
        status = str(stream.get("status", "invalid"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_WORKSTREAM_STATUS",
        "project_root": str(project),
        "workstream_count": len(streams),
        "status_counts": counts,
        "workstreams": streams,
        "invalid_count": len(invalid),
        "invalid": invalid,
        "checked_at": core.utc_now(),
    }
