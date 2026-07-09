"""Read-only status summaries for verification check artifacts."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import core, worker_diagnostics

VERIFICATION_RESULT_KIND = "ORCHESTRATOR_VERIFICATION_RESULT"
CHECKS_STATUS_KIND = "ORCHESTRATOR_CHECKS_STATUS"
CHECK_STATUSES = {"passed", "failed", "errored", "cancelled", "missing", "unknown"}
UNSUCCESSFUL_STATUSES = {"failed", "errored", "cancelled"}


class VerificationError(RuntimeError):
    """A deterministic verification status failure."""


def checks_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "checks"


def checks_status(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    check_id: str | None = None,
    status: str | None = None,
    minimum_severity: str = "info",
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    check_paths = selected_check_paths(project, state_dir=state_dir, check_id=check_id)

    summaries: dict[str, Any] = {}
    all_diagnostics: list[dict[str, str]] = []
    for result_path in check_paths:
        summary = summarize_check(project, result_path)
        if status is not None and summary.get("status") != status:
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
        summaries[str(summary["directory_check_id"])] = summary

    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": CHECKS_STATUS_KIND,
        "checks_root": str(checks_root(project, state_dir=state_dir)),
        "generated_at": core.utc_now(),
        "filters": {
            "check_id": check_id,
            "status": status,
            "minimum_severity": minimum_severity,
        },
        "check_count": len(summaries),
        "status_counts": status_counts(summaries.values()),
        "diagnostic_count": len(all_diagnostics),
        "severity_counts": worker_diagnostics.severity_counts(all_diagnostics),
        "worst_severity": worker_diagnostics.worst_severity(all_diagnostics),
        "checks": summaries,
    }


def selected_check_paths(
    project_root: Path,
    *,
    state_dir: str,
    check_id: str | None,
) -> list[Path]:
    root = checks_root(project_root, state_dir=state_dir)
    if check_id is not None:
        if (
            not check_id
            or "/" in check_id
            or "\\" in check_id
            or check_id.startswith(".")
        ):
            raise VerificationError(f"invalid check id: {check_id!r}")
        path = root / check_id / "verification-result.json"
        if not path.exists() and not path.parent.exists():
            raise VerificationError(f"unknown check: {check_id}")
        return [path]
    if not root.is_dir():
        return []
    return [
        check_dir / "verification-result.json"
        for check_dir in sorted(root.iterdir())
        if check_dir.is_dir()
    ]


def summarize_check(project_root: Path, result_path: Path) -> dict[str, Any]:
    check_dir = result_path.parent
    directory_check_id = check_dir.name
    diagnostics: list[dict[str, str]] = []
    try:
        result = core.load_object(result_path)
    except (OSError, core.OrchestratorError) as error:
        diagnostics.append(
            diagnostic(
                code="verification_result_unreadable",
                severity="error",
                message=(
                    f"verification check {directory_check_id} result is "
                    f"unreadable: {error}"
                ),
                suggested_action=(
                    "Inspect verification-result.json and the check directory; "
                    "do not delete durable logs unless a retention rule allows it."
                ),
            )
        )
        return base_summary(
            directory_check_id=directory_check_id,
            check_id=directory_check_id,
            status="missing" if not result_path.exists() else "unknown",
            result_path=result_path,
            check_dir=check_dir,
            diagnostics=diagnostics,
        )

    check_id = str(result.get("check_id") or directory_check_id)
    status = str(result.get("status") or "unknown")
    artifacts = verification_artifacts(project_root, check_dir, result)
    diagnostics.extend(
        result_diagnostics(
            directory_check_id=directory_check_id,
            check_id=check_id,
            result=result,
            status=status,
        )
    )
    diagnostics.extend(
        artifact_diagnostics(
            check_id=check_id,
            status=status,
            artifacts=artifacts,
        )
    )

    summary = base_summary(
        directory_check_id=directory_check_id,
        check_id=check_id,
        status=status,
        result_path=result_path,
        check_dir=check_dir,
        diagnostics=diagnostics,
    )
    commands = result.get("commands")
    if not isinstance(commands, list):
        commands = []
    command_summaries = [
        command_summary(project_root, command)
        for command in commands
        if isinstance(command, dict)
    ]
    failed_commands = [
        command
        for command in command_summaries
        if command.get("required", True) and command.get("status") != "passed"
    ]
    summary.update(
        {
            "suite": result.get("suite"),
            "exit_code": result.get("exit_code"),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
            "duration_seconds": result.get("duration_seconds"),
            "command_count": len(command_summaries),
            "failed_command_count": len(failed_commands),
            "summary_path": str(artifacts["summary"]),
            "log_path": str(artifacts["full_log"]),
            "commands": command_summaries,
            "failed_commands": failed_commands,
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        }
    )
    return summary


def base_summary(
    *,
    directory_check_id: str,
    check_id: str,
    status: str,
    result_path: Path,
    check_dir: Path,
    diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "directory_check_id": directory_check_id,
        "check_id": check_id,
        "status": status,
        "check_dir": str(check_dir),
        "result_path": str(result_path),
        "diagnostic_count": len(diagnostics),
        "severity_counts": worker_diagnostics.severity_counts(diagnostics),
        "worst_severity": worker_diagnostics.worst_severity(diagnostics),
        "diagnostics": diagnostics,
    }


def verification_artifacts(
    project_root: Path,
    check_dir: Path,
    result: dict[str, Any],
) -> dict[str, Path]:
    return {
        "result": path_from_result(project_root, result.get("result_path"))
        or check_dir / "verification-result.json",
        "summary": path_from_result(project_root, result.get("summary_path"))
        or check_dir / "summary.txt",
        "full_log": path_from_result(project_root, result.get("log_path"))
        or check_dir / "full.log",
    }


def path_from_result(project_root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError:
        return None
    return resolved


def result_diagnostics(
    *,
    directory_check_id: str,
    check_id: str,
    result: dict[str, Any],
    status: str,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if result.get("check_id") != directory_check_id:
        diagnostics.append(
            diagnostic(
                code="verification_check_id_mismatch",
                severity="error",
                message=(
                    f"verification result id {result.get('check_id')!r} does "
                    f"not match directory {directory_check_id!r}"
                ),
                suggested_action=(
                    "Inspect verification-result.json before trusting this check."
                ),
            )
        )
    if not core.is_supported_schema_version(result.get("schema_version")):
        diagnostics.append(
            diagnostic(
                code="verification_schema_unsupported",
                severity="error",
                message=f"verification check {check_id} has unsupported schema",
                suggested_action=(
                    "Upgrade OrchestratorEngine or inspect the verification "
                    "result manually."
                ),
            )
        )
    if result.get("kind") != VERIFICATION_RESULT_KIND:
        diagnostics.append(
            diagnostic(
                code="verification_kind_unexpected",
                severity="warning",
                message=(
                    f"verification check {check_id} kind is not "
                    f"{VERIFICATION_RESULT_KIND}"
                ),
                suggested_action="Inspect verification-result.json manually.",
            )
        )
    if status not in CHECK_STATUSES:
        diagnostics.append(
            diagnostic(
                code="verification_status_unknown",
                severity="warning",
                message=f"verification check {check_id} has unknown status {status!r}",
                suggested_action="Inspect verification-result.json manually.",
            )
        )
    if status in UNSUCCESSFUL_STATUSES:
        diagnostics.append(
            diagnostic(
                code="verification_unsuccessful",
                severity="warning",
                message=f"verification check {check_id} finished with status {status}",
                suggested_action=(
                    "Read summary.txt first, then inspect only failed command logs."
                ),
            )
        )
    commands = result.get("commands")
    if not isinstance(commands, list):
        diagnostics.append(
            diagnostic(
                code="verification_commands_invalid",
                severity="warning",
                message=f"verification check {check_id} commands is not a list",
                suggested_action="Inspect verification-result.json manually.",
            )
        )
    return diagnostics


def artifact_diagnostics(
    *,
    check_id: str,
    status: str,
    artifacts: dict[str, Path],
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    for name, path in artifacts.items():
        if not path.is_file():
            severity = "error" if name in {"result", "summary"} else "warning"
            diagnostics.append(
                diagnostic(
                    code=f"verification_missing_{name}",
                    severity=severity,
                    message=f"verification check {check_id} is missing {name}",
                    suggested_action=f"Inspect check artifacts and recover {path}.",
                )
            )
    return diagnostics


def command_summary(project_root: Path, command: dict[str, Any]) -> dict[str, Any]:
    log_path = path_from_result(project_root, command.get("log_path"))
    return {
        "label": command.get("label"),
        "required": command.get("required", True),
        "status": command.get("status"),
        "exit_code": command.get("exit_code"),
        "duration_seconds": command.get("duration_seconds"),
        "command": command.get("command"),
        "log_path": str(log_path) if log_path is not None else None,
        "output_line_count": command.get("output_line_count"),
        "error": command.get("error"),
    }


def status_counts(summaries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(CHECK_STATUSES)}
    for summary in summaries:
        status = summary.get("status")
        if isinstance(status, str) and status in counts:
            counts[status] += 1
        else:
            counts["unknown"] += 1
    return counts


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
