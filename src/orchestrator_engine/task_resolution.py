"""Operator resolutions for historical worker task outcomes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import core, workers

TASK_RESOLUTION_KIND = "WORKER_TASK_RESOLUTION"
RESOLUTION_STATUSES = {"acknowledged", "superseded"}


class TaskResolutionError(RuntimeError):
    """A deterministic task resolution failure."""


def resolutions_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "task-resolutions"


def resolution_path_for(
    project_root: Path,
    task_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    validate_task_id(task_id)
    return resolutions_root(project_root, state_dir=state_dir) / f"{task_id}.json"


def validate_task_id(task_id: str) -> None:
    if not task_id or "/" in task_id or "\\" in task_id or task_id.startswith("."):
        raise TaskResolutionError(f"invalid task id: {task_id!r}")


def write_resolution(
    project_root: Path,
    *,
    task_id: str,
    status: str,
    reason: str,
    superseded_by_task_id: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
    replace: bool = False,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    validate_task_id(task_id)
    if status not in RESOLUTION_STATUSES:
        raise TaskResolutionError(f"unsupported task resolution status: {status}")
    if not reason.strip():
        raise TaskResolutionError("reason is required")
    if status == "superseded":
        if not superseded_by_task_id:
            raise TaskResolutionError(
                "superseded_by_task_id is required for superseded resolutions"
            )
        validate_task_id(superseded_by_task_id)
        if superseded_by_task_id == task_id:
            raise TaskResolutionError("a task cannot supersede itself")
        superseding_task = load_task_descriptor(
            project,
            superseded_by_task_id,
            state_dir=state_dir,
        )
        if superseding_task.get("status") != "completed":
            raise TaskResolutionError(
                "superseded_by_task_id must reference a completed task"
            )
    elif superseded_by_task_id is not None:
        raise TaskResolutionError(
            "superseded_by_task_id is only valid for superseded resolutions"
        )
    task = load_task_descriptor(project, task_id, state_dir=state_dir)
    previous_status = task.get("status")
    if previous_status not in (core.TERMINAL_STATUSES - {"completed"}):
        raise TaskResolutionError(
            "task resolution requires an unsuccessful terminal task status"
        )

    path = resolution_path_for(project, task_id, state_dir=state_dir)
    previous_resolution = None
    if path.exists() and not replace:
        raise TaskResolutionError(
            f"task resolution already exists: {path}; pass --replace to update it"
        )
    if path.exists():
        previous_resolution = load_resolution(project, task_id, state_dir=state_dir)
    resolution: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": TASK_RESOLUTION_KIND,
        "task_id": task_id,
        "status": status,
        "reason": reason.strip(),
        "previous_task_status": previous_status,
        "created_at": core.utc_now(),
    }
    if superseded_by_task_id is not None:
        resolution["superseded_by_task_id"] = superseded_by_task_id
    if previous_resolution is not None:
        resolution["previous_resolution"] = {
            key: value
            for key, value in previous_resolution.items()
            if key != "previous_resolution"
        }
    core.atomic_json(path, resolution)
    return {**resolution, "resolution_path": str(path)}


def load_task_descriptor(
    project_root: Path,
    task_id: str,
    *,
    state_dir: str,
) -> dict[str, Any]:
    task_dir = workers.task_dir_for(project_root, task_id, state_dir=state_dir)
    if not task_dir.exists():
        raise TaskResolutionError(f"unknown task: {task_id}")
    task_path = task_dir / "task.json"
    try:
        task = core.load_object(task_path)
    except core.OrchestratorError as error:
        raise TaskResolutionError(
            f"task descriptor is unreadable: {task_path}: {error}"
        ) from error
    if not core.is_supported_schema_version(task.get("schema_version")):
        raise TaskResolutionError(
            f"task descriptor has unsupported schema: {task_path}"
        )
    if task.get("kind") != workers.TASK_KIND:
        raise TaskResolutionError(f"task descriptor has unsupported kind: {task_path}")
    if task.get("task_id") != task_id:
        raise TaskResolutionError(
            f"task descriptor id does not match {task_id}: {task_path}"
        )
    return task


def load_resolution(
    project_root: Path,
    task_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any] | None:
    path = resolution_path_for(project_root, task_id, state_dir=state_dir)
    if not path.is_file():
        return None
    resolution = core.load_object(path)
    validate_resolution(resolution, path=path)
    return {**resolution, "resolution_path": str(path)}


def validate_resolution(resolution: dict[str, Any], *, path: Path) -> None:
    if not core.is_supported_schema_version(resolution.get("schema_version")):
        raise TaskResolutionError(f"unsupported task resolution schema: {path}")
    if resolution.get("kind") != TASK_RESOLUTION_KIND:
        raise TaskResolutionError(f"unsupported task resolution kind: {path}")
    task_id = resolution.get("task_id")
    status = resolution.get("status")
    reason = resolution.get("reason")
    previous_task_status = resolution.get("previous_task_status")
    if not isinstance(task_id, str):
        raise TaskResolutionError(f"task resolution has invalid task_id: {path}")
    validate_task_id(task_id)
    if status not in RESOLUTION_STATUSES:
        raise TaskResolutionError(f"task resolution has invalid status: {path}")
    if not isinstance(reason, str) or not reason.strip():
        raise TaskResolutionError(f"task resolution has invalid reason: {path}")
    if previous_task_status not in (core.TERMINAL_STATUSES - {"completed"}):
        raise TaskResolutionError(
            f"task resolution has invalid previous_task_status: {path}"
        )
    superseded_by = resolution.get("superseded_by_task_id")
    if status == "superseded":
        if not isinstance(superseded_by, str):
            raise TaskResolutionError(
                f"superseded task resolution is missing superseded_by_task_id: {path}"
            )
        validate_task_id(superseded_by)
        if superseded_by == task_id:
            raise TaskResolutionError(f"task resolution supersedes itself: {path}")
    elif superseded_by is not None:
        raise TaskResolutionError(
            f"non-superseded task resolution has superseded_by_task_id: {path}"
        )


def list_resolutions(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    root = resolutions_root(project, state_dir=state_dir)
    items: dict[str, Any] = {}
    invalid: list[dict[str, str]] = []
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            try:
                resolution = core.load_object(path)
                validate_resolution(resolution, path=path)
            except (OSError, core.OrchestratorError, TaskResolutionError) as error:
                invalid.append({"path": str(path), "error": str(error)})
                continue
            items[str(resolution["task_id"])] = {
                **resolution,
                "resolution_path": str(path),
            }
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_TASK_RESOLUTION_LIST",
        "resolutions_root": str(root),
        "generated_at": core.utc_now(),
        "resolution_count": len(items),
        "invalid_count": len(invalid),
        "invalid": invalid,
        "resolutions": items,
    }
