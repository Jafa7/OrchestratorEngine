"""Compact read-only operator status aggregation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    __version__,
    core,
    diagnostics,
    task_diagnostics,
    verification,
    worker_diagnostics,
)

STATUS_KIND = "ORCHESTRATOR_STATUS_REPORT"


class StatusError(RuntimeError):
    """A deterministic aggregate status failure."""


def run_status(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    host: str | None = None,
    minimum_severity: str = "warning",
    stale_after_seconds: float = task_diagnostics.DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    doctor = diagnostics.run_doctor(project, state_dir=state_dir, host=host)
    tasks = task_diagnostics.diagnose_tasks(
        project,
        state_dir=state_dir,
        minimum_severity=minimum_severity,
        stale_after_seconds=stale_after_seconds,
    )
    checks = verification.checks_status(
        project,
        state_dir=state_dir,
        minimum_severity=minimum_severity,
    )
    wake_channel = summarize_wake_channel(doctor)
    worker_profiles = summarize_worker_profiles(doctor)
    components = {
        "doctor": summarize_doctor(doctor),
        "worker_profiles": worker_profiles,
        "wake_channel": wake_channel,
        "worker_tasks": summarize_worker_tasks(tasks),
        "checks": summarize_checks(checks),
    }
    issues = collect_issues(
        doctor=doctor,
        tasks=tasks,
        checks=checks,
        wake_channel=wake_channel,
    )
    worst = worst_component_severity(components.values())
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": STATUS_KIND,
        "project_root": str(project),
        "state_dir": state_dir,
        "engine_version": __version__,
        "generated_at": core.utc_now(),
        "filters": {
            "host": host,
            "minimum_severity": minimum_severity,
            "stale_after_seconds": stale_after_seconds,
        },
        "status": status_from_severity(worst),
        "worst_severity": worst,
        "components": components,
        "issue_count": len(issues),
        "issues": issues,
    }


def summarize_doctor(report: dict[str, Any]) -> dict[str, Any]:
    checks = [
        {
            "name": item.get("name"),
            "status": item.get("status"),
            "detail": item.get("detail"),
            "hint": item.get("hint"),
        }
        for item in report.get("checks", [])
        if isinstance(item, dict)
    ]
    return {
        "status": report.get("status"),
        "worst_severity": severity_from_doctor_status(report.get("status")),
        "check_count": len(checks),
        "checks": checks,
    }


def summarize_worker_profiles(report: dict[str, Any]) -> dict[str, Any]:
    check = doctor_check(report, "workers")
    data = check.get("data", {}) if check else {}
    warnings = data.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    return {
        "status": check.get("status") if check else "skipped",
        "worst_severity": severity_from_doctor_status(
            check.get("status") if check else "skipped"
        ),
        "worker_count": data.get("worker_count", 0),
        "enabled_count": data.get("enabled_count", 0),
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def summarize_wake_channel(report: dict[str, Any]) -> dict[str, Any]:
    check = doctor_check(report, "watcher_channel")
    data = check.get("data", {}) if check else {}
    summary: dict[str, Any] = {
        "status": check.get("status") if check else "skipped",
        "worst_severity": severity_from_doctor_status(
            check.get("status") if check else "skipped"
        ),
        "host": data.get("host"),
        "detail": check.get("detail") if check else None,
        "hint": check.get("hint") if check else None,
    }
    service = data.get("service_status")
    if isinstance(service, dict):
        summary["service_status"] = {
            "status": service.get("status"),
            "alive": service.get("alive"),
            "pending_inbox_count": service.get("pending_inbox_count"),
            "deferred_event_count": service.get("deferred_event_count"),
            "manual_required_count": service.get("manual_required_count"),
            "warnings": service.get("warnings", []),
            "service_file": service.get("service_file"),
            "state_path": service.get("state_path"),
        }
    stream = data.get("stream_status")
    if isinstance(stream, dict):
        summary["stream_status"] = {
            "status": stream.get("status"),
            "healthy": stream.get("healthy"),
            "pending_inbox_count": stream.get("pending_inbox_count"),
            "last_error": stream.get("last_error"),
            "state_path": stream.get("state_path"),
        }
    return summary


def summarize_worker_tasks(report: dict[str, Any]) -> dict[str, Any]:
    tasks = report.get("tasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
    problem_tasks = {
        task_id: {
            "task_id": task.get("task_id"),
            "worker": task.get("worker"),
            "status": task.get("status"),
            "finished_at": task.get("finished_at"),
            "diagnostic_count": task.get("diagnostic_count", 0),
            "diagnostics": task.get("diagnostics", []),
            "artifacts": task.get("artifacts", {}),
        }
        for task_id, task in tasks.items()
        if isinstance(task, dict) and task.get("diagnostic_count", 0)
    }
    return {
        "status": status_from_severity(report.get("worst_severity")),
        "worst_severity": report.get("worst_severity"),
        "task_count": report.get("task_count", 0),
        "status_counts": report.get("status_counts", {}),
        "diagnostic_count": report.get("diagnostic_count", 0),
        "problem_task_count": len(problem_tasks),
        "problem_tasks": problem_tasks,
    }


def summarize_checks(report: dict[str, Any]) -> dict[str, Any]:
    checks = report.get("checks", {})
    if not isinstance(checks, dict):
        checks = {}
    problem_checks = {
        check_id: {
            "check_id": check.get("check_id"),
            "status": check.get("status"),
            "summary_path": check.get("summary_path"),
            "failed_command_count": check.get("failed_command_count", 0),
            "failed_commands": check.get("failed_commands", []),
            "diagnostic_count": check.get("diagnostic_count", 0),
            "diagnostics": check.get("diagnostics", []),
        }
        for check_id, check in checks.items()
        if isinstance(check, dict)
        and (check.get("diagnostic_count", 0) or check.get("status") != "passed")
    }
    return {
        "status": status_from_severity(report.get("worst_severity")),
        "worst_severity": report.get("worst_severity"),
        "check_count": report.get("check_count", 0),
        "status_counts": report.get("status_counts", {}),
        "diagnostic_count": report.get("diagnostic_count", 0),
        "problem_check_count": len(problem_checks),
        "problem_checks": problem_checks,
    }


def collect_issues(
    *,
    doctor: dict[str, Any],
    tasks: dict[str, Any],
    checks: dict[str, Any],
    wake_channel: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for item in doctor.get("checks", []):
        if not isinstance(item, dict) or item.get("status") not in {"warn", "error"}:
            continue
        issues.append(
            {
                "source": "doctor",
                "severity": severity_from_doctor_status(item.get("status")),
                "name": item.get("name"),
                "message": item.get("detail"),
                "suggested_action": item.get("hint"),
            }
        )
    for warning in wake_channel_warnings(wake_channel):
        issues.append(
            {
                "source": "wake_channel",
                "severity": "warning",
                "message": warning,
                "suggested_action": wake_channel.get("hint"),
            }
        )
    issues.extend(diagnostic_issues("worker_tasks", tasks.get("tasks", {})))
    issues.extend(diagnostic_issues("checks", checks.get("checks", {})))
    return issues


def diagnostic_issues(source: str, items: object) -> list[dict[str, Any]]:
    if not isinstance(items, dict):
        return []
    issues: list[dict[str, Any]] = []
    id_key = "task_id" if source == "worker_tasks" else "check_id"
    for item_id, item in items.items():
        if not isinstance(item, dict):
            continue
        for diagnostic in item.get("diagnostics", []):
            if not isinstance(diagnostic, dict):
                continue
            issues.append(
                {
                    "source": source,
                    id_key: item.get(id_key) or item_id,
                    "severity": diagnostic.get("severity"),
                    "code": diagnostic.get("code"),
                    "message": diagnostic.get("message"),
                    "suggested_action": diagnostic.get("suggested_action"),
                }
            )
    return issues


def wake_channel_warnings(wake_channel: dict[str, Any]) -> list[str]:
    service = wake_channel.get("service_status")
    if isinstance(service, dict) and isinstance(service.get("warnings"), list):
        return [str(item) for item in service["warnings"]]
    return []


def doctor_check(report: dict[str, Any], name: str) -> dict[str, Any] | None:
    for item in report.get("checks", []):
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def severity_from_doctor_status(status: object) -> str | None:
    if status == "error":
        return "error"
    if status == "warn":
        return "warning"
    return None


def status_from_severity(severity: object) -> str:
    if severity == "error":
        return "error"
    if severity == "warning":
        return "warn"
    return "ok"


def worst_component_severity(components: object) -> str | None:
    diagnostics = []
    for component in components:
        if isinstance(component, dict):
            severity = component.get("worst_severity")
            if isinstance(severity, str):
                diagnostics.append(
                    worker_diagnostics.diagnostic(
                        code="component_status",
                        severity=severity,
                        message="component status",
                        suggested_action="inspect component",
                    )
                )
    return worker_diagnostics.worst_severity(diagnostics)


def exit_code(report: dict[str, Any]) -> int:
    return worker_diagnostics.exit_code_for_worst(report.get("worst_severity"))
