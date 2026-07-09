"""Read-only diagnostics for detached worker task artifacts."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import core, worker_diagnostics, workers

TASK_DIAGNOSTICS_KIND = "WORKER_TASK_DIAGNOSTICS"
RUNNING_STATUSES = {"starting", "running"}
UNSUCCESSFUL_TERMINAL_STATUSES = core.TERMINAL_STATUSES - {"completed"}
DEFAULT_STALE_AFTER_SECONDS = workers.TASK_HEARTBEAT_INTERVAL_SECONDS * 3
TASK_STATUSES = RUNNING_STATUSES | core.TERMINAL_STATUSES
ProcessChecker = Callable[[int], bool]


class TaskDiagnosticError(RuntimeError):
    """A deterministic worker task diagnostic failure."""


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


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def age_seconds(value: object, *, now: datetime) -> float | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return max((now - parsed).total_seconds(), 0.0)


def diagnostic(
    *,
    code: str,
    severity: str,
    message: str,
    suggested_action: str,
) -> dict[str, str]:
    return worker_diagnostics.diagnostic(
        code=code,
        severity=severity,
        message=message,
        suggested_action=suggested_action,
    )


def diagnose_tasks(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    task_id: str | None = None,
    worker: str | None = None,
    status: str | None = None,
    minimum_severity: str = "info",
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    process_checker: ProcessChecker = process_alive,
    now: datetime | None = None,
) -> dict[str, Any]:
    if stale_after_seconds <= 0:
        raise TaskDiagnosticError("stale_after_seconds must be positive")
    project = project_root.expanduser().resolve()
    current = now or datetime.now(UTC)
    task_paths = selected_task_paths(project, state_dir=state_dir, task_id=task_id)

    summaries: dict[str, Any] = {}
    all_diagnostics: list[dict[str, str]] = []
    for descriptor_path in task_paths:
        summary = summarize_task(
            project,
            descriptor_path,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
            process_checker=process_checker,
            now=current,
        )
        if status is not None and summary.get("status") != status:
            continue
        if worker is not None and summary.get("worker") != worker:
            continue
        filtered = worker_diagnostics.filter_diagnostics(
            summary["diagnostics"],
            minimum_severity=minimum_severity,
        )
        summary["diagnostics"] = filtered
        summary["diagnostic_count"] = len(filtered)
        summary["severity_counts"] = worker_diagnostics.severity_counts(filtered)
        summary["worst_severity"] = worker_diagnostics.worst_severity(filtered)
        all_diagnostics.extend(filtered)
        summaries[str(summary["directory_task_id"])] = summary

    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": TASK_DIAGNOSTICS_KIND,
        "tasks_root": str(workers.tasks_root(project, state_dir=state_dir)),
        "generated_at": core.utc_now(),
        "filters": {
            "task_id": task_id,
            "worker": worker,
            "status": status,
            "minimum_severity": minimum_severity,
            "stale_after_seconds": stale_after_seconds,
        },
        "task_count": len(summaries),
        "status_counts": status_counts(summaries.values()),
        "diagnostic_count": len(all_diagnostics),
        "severity_counts": worker_diagnostics.severity_counts(all_diagnostics),
        "worst_severity": worker_diagnostics.worst_severity(all_diagnostics),
        "tasks": summaries,
    }


def status_counts(summaries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(TASK_STATUSES)}
    unknown_count = 0
    for summary in summaries:
        status = summary.get("status")
        if isinstance(status, str) and status in counts:
            counts[status] += 1
        else:
            unknown_count += 1
    if unknown_count:
        counts["unknown"] = unknown_count
    return counts


def selected_task_paths(
    project_root: Path,
    *,
    state_dir: str,
    task_id: str | None,
) -> list[Path]:
    root = workers.tasks_root(project_root, state_dir=state_dir)
    if task_id is not None:
        if not task_id or "/" in task_id or "\\" in task_id or task_id.startswith("."):
            raise TaskDiagnosticError(f"invalid task id: {task_id!r}")
        path = root / task_id / "task.json"
        if not path.exists() and not path.parent.exists():
            raise TaskDiagnosticError(f"unknown task: {task_id}")
        return [path]
    if not root.is_dir():
        return []
    return [
        task_dir / "task.json"
        for task_dir in sorted(root.iterdir())
        if task_dir.is_dir()
    ]


def summarize_task(
    project_root: Path,
    descriptor_path: Path,
    *,
    state_dir: str,
    stale_after_seconds: float,
    process_checker: ProcessChecker,
    now: datetime,
) -> dict[str, Any]:
    task_dir = descriptor_path.parent
    descriptor: dict[str, Any]
    diagnostics: list[dict[str, str]] = []
    try:
        descriptor = core.load_object(descriptor_path)
    except (OSError, core.OrchestratorError) as error:
        task_id = task_dir.name
        diagnostics.append(
            diagnostic(
                code="task_descriptor_unreadable",
                severity="error",
                message=f"task {task_id} descriptor is unreadable: {error}",
                suggested_action=(
                    "Inspect or repair task.json manually; do not delete "
                    "durable task artifacts unless a retention rule allows it."
                ),
            )
        )
        return base_summary(
            task_id=task_id,
            descriptor_path=descriptor_path,
            task_dir=task_dir,
            diagnostics=diagnostics,
        )

    task_id = str(descriptor.get("task_id") or task_dir.name)
    status = str(descriptor.get("status") or "unknown")
    artifacts = task_artifacts(task_dir, descriptor)
    supervisor_pid = descriptor.get("supervisor_pid")
    worker_pid = descriptor.get("worker_pid")
    if status in RUNNING_STATUSES:
        supervisor_alive = (
            process_checker(supervisor_pid)
            if isinstance(supervisor_pid, int)
            else None
        )
        worker_alive = (
            process_checker(worker_pid) if isinstance(worker_pid, int) else None
        )
        heartbeat_age = age_seconds(
            descriptor.get("last_alive_at") or descriptor.get("created_at"),
            now=now,
        )
    else:
        supervisor_alive = None
        worker_alive = None
        heartbeat_age = None

    diagnostics.extend(
        descriptor_diagnostics(
            task_id=task_id,
            descriptor=descriptor,
            descriptor_path=descriptor_path,
            directory_task_id=task_dir.name,
            status=status,
        )
    )
    diagnostics.extend(
        running_task_diagnostics(
            task_id=task_id,
            status=status,
            supervisor_pid=supervisor_pid,
            supervisor_alive=supervisor_alive,
            worker_pid=worker_pid,
            worker_alive=worker_alive,
            heartbeat_age=heartbeat_age,
            stale_after_seconds=stale_after_seconds,
        )
    )
    diagnostics.extend(
        terminal_task_diagnostics(
            task_id=task_id,
            status=status,
            task_dir=task_dir,
            artifacts=artifacts,
        )
    )

    summary = base_summary(
        task_id=task_id,
        descriptor_path=descriptor_path,
        task_dir=task_dir,
        diagnostics=diagnostics,
    )
    summary.update(
        {
            "worker": descriptor.get("worker"),
            "status": status,
            "created_at": descriptor.get("created_at"),
            "finished_at": descriptor.get("finished_at"),
            "last_alive_at": descriptor.get("last_alive_at"),
            "heartbeat_age_seconds": (
                round(heartbeat_age, 3) if heartbeat_age is not None else None
            ),
            "supervisor_pid": supervisor_pid,
            "supervisor_alive": supervisor_alive,
            "worker_pid": worker_pid,
            "worker_alive": worker_alive,
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        }
    )
    return summary


def base_summary(
    *,
    task_id: str,
    descriptor_path: Path,
    task_dir: Path,
    diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "directory_task_id": task_dir.name,
        "task_dir": str(task_dir),
        "descriptor_path": str(descriptor_path),
        "diagnostic_count": len(diagnostics),
        "severity_counts": worker_diagnostics.severity_counts(diagnostics),
        "worst_severity": worker_diagnostics.worst_severity(diagnostics),
        "diagnostics": diagnostics,
    }


def task_artifacts(task_dir: Path, descriptor: dict[str, Any]) -> dict[str, Path]:
    artifacts = {
        "result": path_from_descriptor(descriptor, "result_path")
        or task_dir / "result.json",
        "evidence": path_from_descriptor(descriptor, "evidence_path")
        or task_dir / "evidence.json",
        "stdout": task_dir / "worker-stdout.log",
        "stderr": task_dir / "worker-stderr.log",
    }
    for key in ("event_path", "signal_path"):
        path = path_from_descriptor(descriptor, key)
        if path is not None:
            artifacts[key.removesuffix("_path")] = path
    return artifacts


def path_from_descriptor(descriptor: dict[str, Any], key: str) -> Path | None:
    value = descriptor.get(key)
    if not isinstance(value, str) or not value:
        return None
    return Path(value).expanduser().resolve()


def descriptor_diagnostics(
    *,
    task_id: str,
    descriptor: dict[str, Any],
    descriptor_path: Path,
    directory_task_id: str,
    status: str,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if descriptor.get("task_id") != directory_task_id:
        diagnostics.append(
            diagnostic(
                code="task_id_mismatch",
                severity="error",
                message=(
                    f"task descriptor id {descriptor.get('task_id')!r} does "
                    f"not match directory {directory_task_id!r}"
                ),
                suggested_action=(
                    "Inspect task.json and task directory before trusting or "
                    "retrying this task."
                ),
            )
        )
    if not core.is_supported_schema_version(descriptor.get("schema_version")):
        diagnostics.append(
            diagnostic(
                code="task_schema_unsupported",
                severity="error",
                message=f"task {task_id} has unsupported schema_version",
                suggested_action=(
                    "Upgrade OrchestratorEngine or inspect the task descriptor "
                    f"manually: {descriptor_path}"
                ),
            )
        )
    if descriptor.get("kind") != workers.TASK_KIND:
        diagnostics.append(
            diagnostic(
                code="task_kind_unexpected",
                severity="warning",
                message=f"task {task_id} descriptor kind is not {workers.TASK_KIND}",
                suggested_action="Inspect task.json before trusting this summary.",
            )
        )
    if status not in TASK_STATUSES:
        diagnostics.append(
            diagnostic(
                code="task_status_unknown",
                severity="warning",
                message=f"task {task_id} has unknown status {status!r}",
                suggested_action="Inspect task.json and result.json manually.",
            )
        )
    return diagnostics


def running_task_diagnostics(
    *,
    task_id: str,
    status: str,
    supervisor_pid: object,
    supervisor_alive: bool | None,
    worker_pid: object,
    worker_alive: bool | None,
    heartbeat_age: float | None,
    stale_after_seconds: float,
) -> list[dict[str, str]]:
    if status not in RUNNING_STATUSES:
        return []
    diagnostics: list[dict[str, str]] = []
    if not isinstance(supervisor_pid, int):
        diagnostics.append(
            diagnostic(
                code="task_running_without_supervisor_pid",
                severity="warning",
                message=f"task {task_id} is {status} without supervisor_pid",
                suggested_action="Inspect supervisor.log and task.json.",
            )
        )
    elif supervisor_alive is False:
        diagnostics.append(
            diagnostic(
                code="task_supervisor_dead",
                severity="error",
                message=(
                    f"task {task_id} is {status} but supervisor pid "
                    f"{supervisor_pid} is not alive"
                ),
                suggested_action=(
                    "Inspect supervisor.log, worker stdout/stderr and rerun or "
                    "mark the task manually if needed."
                ),
            )
        )
    if isinstance(worker_pid, int) and worker_alive is False:
        diagnostics.append(
            diagnostic(
                code="task_worker_dead",
                severity="warning",
                message=(
                    f"task {task_id} is {status} but worker pid "
                    f"{worker_pid} is not alive"
                ),
                suggested_action=(
                    "Wait for the supervisor to finalize the task; if it does "
                    "not, inspect supervisor.log."
                ),
            )
        )
    if heartbeat_age is None:
        diagnostics.append(
            diagnostic(
                code="task_running_without_heartbeat",
                severity="warning",
                message=f"task {task_id} is {status} without a timestamp",
                suggested_action="Inspect task.json and supervisor.log manually.",
            )
        )
    elif heartbeat_age > stale_after_seconds:
        diagnostics.append(
            diagnostic(
                code="task_heartbeat_stale",
                severity="warning",
                message=(
                    f"task {task_id} heartbeat age {heartbeat_age:.1f}s "
                    f"exceeds {stale_after_seconds:.1f}s"
                ),
                suggested_action=(
                    "If the supervisor is alive, wait or inspect logs; if it is "
                    "dead, treat the task as crashed."
                ),
            )
        )
    return diagnostics


def terminal_task_diagnostics(
    *,
    task_id: str,
    status: str,
    task_dir: Path,
    artifacts: dict[str, Path],
) -> list[dict[str, str]]:
    if status not in core.TERMINAL_STATUSES:
        return []
    diagnostics: list[dict[str, str]] = []
    if status in UNSUCCESSFUL_TERMINAL_STATUSES:
        diagnostics.append(
            diagnostic(
                code="task_terminal_unsuccessful",
                severity="warning",
                message=f"task {task_id} finished with terminal status {status}",
                suggested_action=(
                    "Read result.json, then worker stdout/stderr as needed."
                ),
            )
        )
    for artifact_name in ("result", "evidence"):
        path = artifacts[artifact_name]
        if not path.is_file():
            diagnostics.append(
                diagnostic(
                    code=f"task_missing_{artifact_name}",
                    severity="error",
                    message=(
                        f"task {task_id} is terminal but {artifact_name} is missing"
                    ),
                    suggested_action=f"Inspect {task_dir} and recover {path}.",
                )
            )
            continue
        try:
            core.load_object(path)
        except (OSError, core.OrchestratorError) as error:
            diagnostics.append(
                diagnostic(
                    code=f"task_unreadable_{artifact_name}",
                    severity="error",
                    message=(
                        f"task {task_id} terminal {artifact_name} is unreadable: "
                        f"{error}"
                    ),
                    suggested_action=f"Inspect and repair {path}.",
                )
            )
    for artifact_name in ("event", "signal"):
        path = artifacts.get(artifact_name)
        if path is not None and not path.is_file():
            diagnostics.append(
                diagnostic(
                    code=f"task_missing_{artifact_name}",
                    severity="error",
                    message=f"task {task_id} references missing {artifact_name}",
                    suggested_action=(
                        "Inspect durable audit artifacts; do not delete related "
                        "result/evidence files."
                    ),
                )
            )
    return diagnostics
