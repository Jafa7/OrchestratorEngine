"""Bounded local waiting across heterogeneous orchestration operations."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import core, github_actions, github_pull_requests, local_checks, workers

SUPPORTED_TARGET_KINDS = {"worker", "check", "ci", "pr"}
WAIT_MODES = {"all", "any"}
MAX_WAIT_TARGETS = 64
ACTIVE_STATUSES = {"starting", "running", "cancelling"}
BROKEN_STATUSES = {"crashed", "stalled", "invalid"}


class OperationWaitError(RuntimeError):
    """A heterogeneous wait target or state is invalid."""


def parse_target(value: str) -> tuple[str, str]:
    target_kind, separator, operation_id = value.partition(":")
    if not separator or target_kind not in SUPPORTED_TARGET_KINDS or not operation_id:
        supported = ", ".join(sorted(SUPPORTED_TARGET_KINDS))
        raise OperationWaitError(
            f"target must use KIND:ID with KIND in: {supported}"
        )
    return target_kind, operation_id


def validate_targets(targets: list[str], *, mode: str) -> None:
    if not targets:
        raise OperationWaitError("operation wait requires at least one target")
    if len(targets) > MAX_WAIT_TARGETS:
        raise OperationWaitError(
            f"operation wait supports at most {MAX_WAIT_TARGETS} targets"
        )
    parsed = [parse_target(target) for target in targets]
    if len(set(parsed)) != len(parsed):
        raise OperationWaitError("operation wait targets must be unique")
    if mode not in WAIT_MODES:
        raise OperationWaitError(
            f"operation wait mode must be one of: {', '.join(sorted(WAIT_MODES))}"
        )


def operation_snapshot(
    project_root: Path,
    *,
    target: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    stale_after_seconds: float = workers.TASK_HEARTBEAT_INTERVAL_SECONDS * 3,
) -> dict[str, Any]:
    target_kind, operation_id = parse_target(target)
    if target_kind == "worker":
        item = workers.worker_wait_snapshot(
            project_root,
            task_id=operation_id,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
        )
        status = str(item.get("status") or "unknown")
        terminal = bool(item.get("terminal"))
        action_required = item.get("health") is not None
        successful = terminal and status == "completed"
    elif target_kind == "check":
        report = local_checks.check_status(
            project_root,
            check_id=operation_id,
            state_dir=state_dir,
        )
        item = _single_report_item(report, key="checks", operation_id=operation_id)
        status = str(item.get("status") or "unknown")
        terminal = status in local_checks.TERMINAL_STATUSES
        action_required = _requires_action(status, terminal=terminal)
        successful = terminal and status == "passed"
    elif target_kind == "ci":
        report = github_actions.monitor_status(
            project_root,
            monitor_id=operation_id,
            state_dir=state_dir,
        )
        item = _single_report_item(report, key="monitors", operation_id=operation_id)
        status = str(item.get("status") or "unknown")
        terminal = status in github_actions.TERMINAL_MONITOR_STATUSES
        action_required = _requires_action(status, terminal=terminal)
        successful = terminal and status == "completed" and (
            item.get("ci_conclusion") == "success"
        )
    else:
        report = github_pull_requests.monitor_status(
            project_root,
            monitor_id=operation_id,
            state_dir=state_dir,
        )
        item = _single_report_item(report, key="monitors", operation_id=operation_id)
        status = str(item.get("status") or "unknown")
        terminal = status in github_pull_requests.TERMINAL_STATUSES
        action_required = _requires_action(status, terminal=terminal)
        successful = terminal and status in github_pull_requests.SUCCESS_STATUSES

    snapshot = {
        "target": target,
        "target_kind": target_kind,
        "operation_id": operation_id,
        "status": status,
        "terminal": terminal,
        "successful": successful,
        "action_required": action_required,
    }
    for key in (
        "worker",
        "suite",
        "ci_conclusion",
        "failure_kind",
        "result_path",
        "summary_path",
        "evidence_path",
        "event_path",
        "finished_at",
        "duration_seconds",
        "wake_policy",
    ):
        if item.get(key) is not None:
            snapshot[key] = item[key]
    health = item.get("health")
    if isinstance(health, dict):
        snapshot["health"] = {
            key: health.get(key)
            for key in ("status", "message", "heartbeat_age_seconds")
            if health.get(key) is not None
        }
    return snapshot


def _single_report_item(
    report: dict[str, Any], *, key: str, operation_id: str
) -> dict[str, Any]:
    items = report.get(key)
    if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
        return items[0]
    invalid = report.get("invalid")
    if isinstance(invalid, list) and invalid:
        first = invalid[0]
        error = first.get("error") if isinstance(first, dict) else first
        return {
            "status": "invalid",
            "failure_kind": "descriptor_invalid",
            "error": str(error)[:500],
        }
    raise OperationWaitError(f"operation status is unavailable: {operation_id}")


def _requires_action(status: str, *, terminal: bool) -> bool:
    return status in BROKEN_STATUSES or (not terminal and status not in ACTIVE_STATUSES)


def operation_wait_snapshot(
    project_root: Path,
    *,
    targets: list[str],
    mode: str = "all",
    state_dir: str = core.DEFAULT_STATE_DIR,
    stale_after_seconds: float = workers.TASK_HEARTBEAT_INTERVAL_SECONDS * 3,
) -> dict[str, Any]:
    validate_targets(targets, mode=mode)
    snapshots = [
        operation_snapshot(
            project_root,
            target=target,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
        )
        for target in targets
    ]
    terminal = [snapshot for snapshot in snapshots if snapshot["terminal"]]
    successful = [snapshot for snapshot in terminal if snapshot["successful"]]
    unsuccessful = [snapshot for snapshot in terminal if not snapshot["successful"]]
    action_required = [
        snapshot for snapshot in snapshots if snapshot["action_required"]
    ]
    wakeup_enabled = [
        snapshot["target"]
        for snapshot in snapshots
        if snapshot.get("wake_policy", "always") != "never"
    ]
    condition_met = bool(terminal) if mode == "any" else len(terminal) == len(snapshots)
    if action_required:
        status = "action_required"
        wait_status = "action_required"
        suggested_action = (
            "Return to the orchestrating chat and inspect unhealthy operations."
        )
    elif condition_met:
        status = "unsuccessful" if unsuccessful else "completed"
        wait_status = "condition_met"
        suggested_action = (
            "Return to the orchestrating chat to review the operation results."
        )
    else:
        status = "waiting"
        wait_status = "waiting"
        suggested_action = "Keep this command open; it will update when ready."
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_OPERATION_WAIT_STATUS",
        "mode": mode,
        "status": status,
        "wait_status": wait_status,
        "condition_met": condition_met,
        "terminal": condition_met,
        "target_count": len(snapshots),
        "terminal_count": len(terminal),
        "successful_count": len(successful),
        "unsuccessful_count": len(unsuccessful),
        "action_required_count": len(action_required),
        "active_count": sum(
            not snapshot["terminal"] and not snapshot["action_required"]
            for snapshot in snapshots
        ),
        "targets": snapshots,
        "terminal_targets": [snapshot["target"] for snapshot in terminal],
        "action_required_targets": [
            snapshot["target"] for snapshot in action_required
        ],
        "wakeup_enabled_count": len(wakeup_enabled),
        "wakeup_enabled_targets": wakeup_enabled,
        "duplicate_followup_risk": bool(wakeup_enabled),
        "suggested_action": suggested_action,
    }


def wait_for_operations(
    project_root: Path,
    *,
    targets: list[str],
    mode: str = "all",
    state_dir: str = core.DEFAULT_STATE_DIR,
    interval_seconds: float = 2.0,
    timeout_seconds: float | None = None,
    stale_after_seconds: float = workers.TASK_HEARTBEAT_INTERVAL_SECONDS * 3,
    on_update: Callable[[dict[str, Any]], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    validate_targets(targets, mode=mode)
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise OperationWaitError("wait interval must be finite and positive")
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise OperationWaitError("wait timeout must be finite and positive")
    if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
        raise OperationWaitError("wait stale threshold must be finite and positive")

    started = monotonic()
    deadline = started + timeout_seconds if timeout_seconds is not None else None
    while True:
        snapshot = operation_wait_snapshot(
            project_root,
            targets=targets,
            mode=mode,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
        )
        now = monotonic()
        snapshot["waited_seconds"] = round(max(now - started, 0.0), 3)
        if on_update is not None:
            on_update(snapshot)
        if snapshot["condition_met"] or snapshot["wait_status"] == "action_required":
            return snapshot
        if deadline is not None and now >= deadline:
            snapshot["status"] = "waiting"
            snapshot["wait_status"] = "timed_out"
            snapshot["suggested_action"] = (
                "The operation set is still active; re-run this command later."
            )
            if on_update is not None:
                on_update(snapshot)
            return snapshot
        sleep_seconds = interval_seconds
        if deadline is not None:
            sleep_seconds = min(sleep_seconds, max(deadline - now, 0.0))
        sleeper(sleep_seconds)
