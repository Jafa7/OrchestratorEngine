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
            expected = (decision, summary, normalized_next, normalized_waiting_on)
            actual = (
                existing.get("decision"),
                existing.get("summary"),
                existing.get("next_action"),
                existing.get("waiting_on"),
            )
            if actual != expected:
                raise WorkstreamError(
                    "checkpoint id already exists with different content: "
                    f"{checkpoint_id}"
                )
            output = {**existing, "checkpoint_path": str(path), "idempotent": True}
            signal_path = existing.get("signal_path")
            if (
                existing.get("decision") == "continue"
                and isinstance(signal_path, str)
                and not Path(signal_path).is_file()
            ):
                wake_target = existing.get("wake_target")
                output["followup"] = core.write_followup_event(
                    project,
                    operation_id=f"{workstream_id}.{checkpoint_id}",
                    source_kind=SOURCE_KIND,
                    terminal_status="completed",
                    result_path=path.with_name(f"{checkpoint_id}.result.json"),
                    evidence_path=path.with_name(f"{checkpoint_id}.evidence.json"),
                    state_dir=state_dir,
                    event_id=str(existing["event_id"]),
                    wake_target=(
                        wake_target if isinstance(wake_target, dict) else None
                    ),
                    not_before=str(existing["not_before"]),
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
        emit_signal = decision == "continue"
        if emit_signal:
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
        result_path: Path | None = None
        evidence_path: Path | None = None
        if emit_signal:
            due_at = now + timedelta(seconds=float(descriptor["delay_seconds"]))
            checkpoint["not_before"] = due_at.isoformat(timespec="milliseconds")
            result_path = path.with_name(f"{checkpoint_id}.result.json")
            evidence_path = path.with_name(f"{checkpoint_id}.evidence.json")
            core.atomic_json(
                result_path,
                {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": "ORCHESTRATOR_WORKSTREAM_CONTINUATION",
                    "workstream_id": workstream_id,
                    "checkpoint_id": checkpoint_id,
                    "status": "ready",
                    "summary": summary,
                    "next_action": normalized_next,
                    "not_before": checkpoint["not_before"],
                },
            )
            core.atomic_json(
                evidence_path,
                {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": "ORCHESTRATOR_WORKSTREAM_EVIDENCE",
                    "workstream_id": workstream_id,
                    "checkpoint_id": checkpoint_id,
                    "ready_declared": True,
                    "checkpoint_path": str(path),
                    "created_at": created_at,
                },
            )
            checkpoint["event_id"] = core.followup_event_id(
                project,
                source_kind=SOURCE_KIND,
                operation_id=f"{workstream_id}.{checkpoint_id}",
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
        descriptor.update(
            status=(
                "active" if effective_decision == "continue" else effective_decision
            ),
            checkpoint_count=sequence,
            latest_checkpoint_id=checkpoint_id,
            latest_checkpoint_path=str(path),
            updated_at=created_at,
        )
        if emit_signal:
            descriptor["continuation_count"] = int(
                descriptor.get("continuation_count", 0)
            ) + 1
            descriptor["last_scheduled_event_id"] = checkpoint["event_id"]
            descriptor["last_scheduled_wake_at"] = checkpoint["not_before"]
        if normalized_waiting_on is not None:
            descriptor["waiting_on"] = normalized_waiting_on
        else:
            descriptor.pop("waiting_on", None)
        core.atomic_json(
            descriptor_path(project, workstream_id, state_dir=state_dir), descriptor
        )
        if emit_signal:
            assert result_path is not None and evidence_path is not None
            event_result = core.write_followup_event(
                project,
                operation_id=f"{workstream_id}.{checkpoint_id}",
                source_kind=SOURCE_KIND,
                terminal_status="completed",
                result_path=result_path,
                evidence_path=evidence_path,
                state_dir=state_dir,
                event_id=str(checkpoint["event_id"]),
                wake_target=(wake_target if isinstance(wake_target, dict) else None),
                not_before=checkpoint["not_before"],
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
