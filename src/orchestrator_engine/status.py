"""Compact read-only operator status aggregation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    __version__,
    core,
    diagnostics,
    host_capabilities,
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
    large_log_bytes: int = task_diagnostics.DEFAULT_LARGE_LOG_BYTES,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    doctor = diagnostics.run_doctor(project, state_dir=state_dir, host=host)
    tasks = task_diagnostics.diagnose_tasks(
        project,
        state_dir=state_dir,
        minimum_severity=minimum_severity,
        stale_after_seconds=stale_after_seconds,
        large_log_bytes=large_log_bytes,
    )
    checks = verification.checks_status(
        project,
        state_dir=state_dir,
        minimum_severity=minimum_severity,
        large_log_bytes=large_log_bytes,
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
            "large_log_bytes": large_log_bytes,
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
    capabilities = data.get("capabilities")
    if isinstance(capabilities, dict):
        summary["capabilities"] = capabilities
    elif isinstance(data.get("host"), str):
        summary["capabilities"] = host_capabilities.for_host(data["host"])
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
            "resolution": task.get("resolution"),
            "finished_at": task.get("finished_at"),
            "diagnostic_count": task.get("diagnostic_count", 0),
            "diagnostics": task.get("diagnostics", []),
            "artifacts": task.get("artifacts", {}),
            "log_sizes": task.get("log_sizes", {}),
        }
        for task_id, task in tasks.items()
        if isinstance(task, dict) and task.get("diagnostic_count", 0)
    }
    resolved_tasks = {
        task_id: {
            "task_id": task.get("task_id"),
            "worker": task.get("worker"),
            "status": task.get("status"),
            "finished_at": task.get("finished_at"),
            "resolution": task.get("resolution"),
        }
        for task_id, task in tasks.items()
        if isinstance(task, dict) and isinstance(task.get("resolution"), dict)
    }
    large_log_bytes = int(report.get("filters", {}).get("large_log_bytes") or 0)
    large_log_tasks = task_large_log_summaries(tasks, large_log_bytes)
    return {
        "status": status_from_severity(report.get("worst_severity")),
        "worst_severity": report.get("worst_severity"),
        "task_count": report.get("task_count", 0),
        "status_counts": report.get("status_counts", {}),
        "resolution_counts": report.get("resolution_counts", {}),
        "diagnostic_count": report.get("diagnostic_count", 0),
        "resolved_task_count": resolved_task_count(tasks),
        "resolved_tasks": resolved_tasks,
        "large_log_task_count": len(large_log_tasks),
        "large_log_tasks": large_log_tasks,
        "problem_task_count": len(problem_tasks),
        "problem_tasks": problem_tasks,
    }


def resolved_task_count(tasks: dict[str, Any]) -> int:
    return sum(
        1
        for task in tasks.values()
        if isinstance(task, dict) and isinstance(task.get("resolution"), dict)
    )


def task_large_log_summaries(
    tasks: dict[str, Any],
    large_log_bytes: int,
) -> dict[str, Any]:
    if large_log_bytes <= 0:
        return {}
    summaries: dict[str, Any] = {}
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            continue
        log_sizes = task.get("log_sizes")
        if not isinstance(log_sizes, dict):
            continue
        large_logs = {
            name: size
            for name, size in log_sizes.items()
            if isinstance(size, int) and size > large_log_bytes
        }
        if large_logs:
            summaries[str(task_id)] = {
                "task_id": task.get("task_id") or task_id,
                "worker": task.get("worker"),
                "status": task.get("status"),
                "large_logs": large_logs,
            }
    return summaries


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
    large_log_bytes = int(report.get("filters", {}).get("large_log_bytes") or 0)
    large_log_checks = check_large_log_summaries(checks, large_log_bytes)
    return {
        "status": status_from_severity(report.get("worst_severity")),
        "worst_severity": report.get("worst_severity"),
        "check_count": report.get("check_count", 0),
        "status_counts": report.get("status_counts", {}),
        "diagnostic_count": report.get("diagnostic_count", 0),
        "large_log_check_count": len(large_log_checks),
        "large_log_checks": large_log_checks,
        "problem_check_count": len(problem_checks),
        "problem_checks": problem_checks,
    }


def check_large_log_summaries(
    checks: dict[str, Any],
    large_log_bytes: int,
) -> dict[str, Any]:
    if large_log_bytes <= 0:
        return {}
    summaries: dict[str, Any] = {}
    for check_id, check in checks.items():
        if not isinstance(check, dict):
            continue
        log_sizes = check.get("log_sizes")
        if not isinstance(log_sizes, dict):
            continue
        large_logs = {
            name: size
            for name, size in log_sizes.items()
            if isinstance(size, int) and size > large_log_bytes
        }
        if large_logs:
            summaries[str(check_id)] = {
                "check_id": check.get("check_id") or check_id,
                "status": check.get("status"),
                "large_logs": large_logs,
            }
    return summaries


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


def report_draft(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    project_name: str | None = None,
    report_type: str = "runtime-report",
    host: str | None = None,
    minimum_severity: str = "warning",
    stale_after_seconds: float = task_diagnostics.DEFAULT_STALE_AFTER_SECONDS,
    large_log_bytes: int = task_diagnostics.DEFAULT_LARGE_LOG_BYTES,
) -> str:
    report = run_status(
        project_root,
        state_dir=state_dir,
        host=host,
        minimum_severity=minimum_severity,
        stale_after_seconds=stale_after_seconds,
        large_log_bytes=large_log_bytes,
    )
    name = project_name or Path(str(report["project_root"])).name
    label_host = selected_host(report, host)
    recommended_labels = recommended_report_labels(
        project_name=name,
        report_type=report_type,
        host=label_host,
    )
    label_text = ", ".join(f"`{label}`" for label in recommended_labels)
    lines = [
        f"# [{report_type}][{name}] Orchestrator status report",
        "",
        "## Summary",
        "",
        f"- Project: `{name}`",
        "- Project root: omitted by default; add a sanitized path only if needed.",
        f"- Engine version: `{report['engine_version']}`",
        f"- Overall status: `{report['status']}`",
        f"- Worst severity: `{report['worst_severity']}`",
        f"- Generated at: `{report['generated_at']}`",
        f"- Source host: `{label_host or 'unknown'}`",
        f"- Recommended labels: {label_text}",
        "",
        "## Component Status",
        "",
    ]
    components = report.get("components", {})
    if isinstance(components, dict):
        for component_name, component in components.items():
            if not isinstance(component, dict):
                continue
            lines.append(
                "- "
                f"`{component_name}`: status=`{component.get('status')}`, "
                f"worst=`{component.get('worst_severity')}`"
            )
            append_component_details(lines, component_name, component)
    lines.extend(["", "## Issues", ""])
    issues = report.get("issues", [])
    if isinstance(issues, list) and issues:
        for index, issue in enumerate(issues, start=1):
            if not isinstance(issue, dict):
                continue
            lines.append(f"{index}. `{issue.get('source')}` "
                         f"`{issue.get('severity')}`")
            if issue.get("name"):
                lines.append(f"   - name: `{issue['name']}`")
            if issue.get("task_id"):
                lines.append(f"   - task_id: `{issue['task_id']}`")
            if issue.get("check_id"):
                lines.append(f"   - check_id: `{issue['check_id']}`")
            if issue.get("code"):
                lines.append(f"   - code: `{issue['code']}`")
            lines.append(
                "   - message: "
                f"{redact_report_text(issue.get('message'), report['project_root'])}"
            )
            if issue.get("suggested_action"):
                suggested_action = redact_report_text(
                    issue["suggested_action"],
                    report["project_root"],
                )
                lines.append(
                    f"   - suggested action: {suggested_action}"
                )
    else:
        lines.append("No issues at the selected severity.")
    append_large_log_task_details(lines, report)
    append_large_log_check_details(lines, report)
    append_resolved_task_details(lines, report)
    lines.extend(
        [
            "",
            "## Runtime Changes Made",
            "",
            "- None by this report draft command.",
            "",
            "## Product Code Changes",
            "",
            "- None by this report draft command.",
            "",
            "## Requested OrchestratorEngine Action",
            "",
            "- Triage whether the reported issue is adopter runtime setup, "
            "documentation gap or OrchestratorEngine core bug.",
        ]
    )
    return "\n".join(lines) + "\n"


def redact_report_text(value: object, project_root: object) -> str:
    """Replace the adopter root in report prose while preserving useful paths."""

    text = str(value)
    root = str(project_root)
    return text.replace(root, "<project-root>") if root else text


def append_component_details(
    lines: list[str],
    component_name: str,
    component: dict[str, Any],
) -> None:
    if component_name == "wake_channel":
        service = component.get("service_status")
        if isinstance(service, dict):
            lines.append(
                "  - service: "
                f"status=`{service.get('status')}`, "
                f"alive=`{service.get('alive')}`, "
                f"pending=`{service.get('pending_inbox_count')}`, "
                f"deferred=`{service.get('deferred_event_count')}`, "
                f"manual_required=`{service.get('manual_required_count')}`"
            )
        stream = component.get("stream_status")
        if isinstance(stream, dict):
            lines.append(
                "  - stream: "
                f"status=`{stream.get('status')}`, "
                f"healthy=`{stream.get('healthy')}`, "
                f"pending=`{stream.get('pending_inbox_count')}`"
            )
    elif component_name == "worker_tasks":
        lines.append(
            "  - tasks: "
            f"count=`{component.get('task_count')}`, "
            f"diagnostics=`{component.get('diagnostic_count')}`, "
            f"problems=`{component.get('problem_task_count')}`, "
            f"resolved=`{component.get('resolved_task_count')}`, "
            f"large_logs=`{component.get('large_log_task_count')}`"
        )
    elif component_name == "checks":
        lines.append(
            "  - checks: "
            f"count=`{component.get('check_count')}`, "
            f"diagnostics=`{component.get('diagnostic_count')}`, "
            f"problems=`{component.get('problem_check_count')}`, "
            f"large_logs=`{component.get('large_log_check_count')}`"
        )
    elif component_name == "worker_profiles":
        lines.append(
            "  - workers: "
            f"count=`{component.get('worker_count')}`, "
            f"enabled=`{component.get('enabled_count')}`, "
            f"profile_warnings=`{component.get('warning_count')}`"
        )


def append_resolved_task_details(lines: list[str], report: dict[str, Any]) -> None:
    components = report.get("components")
    if not isinstance(components, dict):
        return
    worker_tasks = components.get("worker_tasks")
    if not isinstance(worker_tasks, dict):
        return
    resolved_tasks = worker_tasks.get("resolved_tasks")
    if not isinstance(resolved_tasks, dict) or not resolved_tasks:
        return
    lines.extend(["", "## Resolved Historical Tasks", ""])
    for index, (task_id, task) in enumerate(sorted(resolved_tasks.items()), start=1):
        if not isinstance(task, dict):
            continue
        resolution = task.get("resolution")
        resolution_status = None
        superseded_by = None
        if isinstance(resolution, dict):
            resolution_status = resolution.get("status")
            superseded_by = resolution.get("superseded_by_task_id")
        lines.append(
            f"{index}. task_id=`{task_id}`, "
            f"status=`{task.get('status')}`, "
            f"resolution=`{resolution_status}`"
        )
        if superseded_by:
            lines.append(f"   - superseded_by_task_id: `{superseded_by}`")


def append_large_log_task_details(lines: list[str], report: dict[str, Any]) -> None:
    components = report.get("components")
    if not isinstance(components, dict):
        return
    worker_tasks = components.get("worker_tasks")
    if not isinstance(worker_tasks, dict):
        return
    large_log_tasks = worker_tasks.get("large_log_tasks")
    if not isinstance(large_log_tasks, dict) or not large_log_tasks:
        return
    lines.extend(["", "## Large Worker Logs", ""])
    for index, (task_id, task) in enumerate(sorted(large_log_tasks.items()), start=1):
        if not isinstance(task, dict):
            continue
        logs = task.get("large_logs")
        log_text = ""
        if isinstance(logs, dict):
            log_text = ", ".join(
                f"{name}={size} bytes"
                for name, size in sorted(logs.items())
            )
        lines.append(
            f"{index}. task_id=`{task_id}`, "
            f"status=`{task.get('status')}`, logs={log_text}"
        )


def append_large_log_check_details(lines: list[str], report: dict[str, Any]) -> None:
    components = report.get("components")
    if not isinstance(components, dict):
        return
    checks = components.get("checks")
    if not isinstance(checks, dict):
        return
    large_log_checks = checks.get("large_log_checks")
    if not isinstance(large_log_checks, dict) or not large_log_checks:
        return
    lines.extend(["", "## Large Verification Logs", ""])
    for index, (check_id, check) in enumerate(
        sorted(large_log_checks.items()),
        start=1,
    ):
        if not isinstance(check, dict):
            continue
        logs = check.get("large_logs")
        log_text = ""
        if isinstance(logs, dict):
            log_text = ", ".join(
                f"{name}={size} bytes"
                for name, size in sorted(logs.items())
            )
        lines.append(
            f"{index}. check_id=`{check_id}`, "
            f"status=`{check.get('status')}`, logs={log_text}"
        )


def selected_host(report: dict[str, Any], explicit_host: str | None) -> str | None:
    if explicit_host:
        return explicit_host
    components = report.get("components")
    if isinstance(components, dict):
        wake_channel = components.get("wake_channel")
        if isinstance(wake_channel, dict) and isinstance(wake_channel.get("host"), str):
            return wake_channel["host"]
    return None


def recommended_report_labels(
    *,
    project_name: str,
    report_type: str,
    host: str | None,
) -> list[str]:
    labels = ["triage", report_type]
    project = label_slug(project_name)
    if project:
        labels.append(f"project:{project}")
    if host:
        labels.append(f"source:{label_slug(host)}")
    return labels


def label_slug(value: str) -> str:
    result = []
    last_was_separator = False
    for char in value.strip().lower():
        if char.isalnum():
            result.append(char)
            last_was_separator = False
        elif not last_was_separator:
            result.append("-")
            last_was_separator = True
    return "".join(result).strip("-")
