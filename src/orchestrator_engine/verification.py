"""Read-only status summaries for verification check artifacts."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import core, worker_diagnostics

VERIFICATION_RESULT_KIND = "ORCHESTRATOR_VERIFICATION_RESULT"
CHECKS_STATUS_KIND = "ORCHESTRATOR_CHECKS_STATUS"
CHECK_STATUSES = {
    "starting",
    "running",
    "passed",
    "failed",
    "errored",
    "cancelled",
    "missing",
    "unknown",
}
UNSUCCESSFUL_STATUSES = {"failed", "errored", "cancelled"}
DEFAULT_LARGE_LOG_BYTES = 1024 * 1024
CHECK_OWNER_KIND = "ORCHESTRATOR_CHECK_RESULT_OWNER"
CHECK_OWNER_NAME = "operation-owner.json"
CHECK_OWNER_TYPES = {"local_check", "github_actions", "github_pull_request"}


class VerificationError(RuntimeError):
    """A deterministic verification status failure."""


def checks_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "checks"


def _legacy_check_owner(directory: Path) -> str | None:
    descriptor = directory / "check.json"
    if descriptor.is_file():
        with contextlib.suppress(OSError, core.OrchestratorError):
            value = core.load_object(descriptor)
            if value.get("kind") == "ORCHESTRATOR_LOCAL_CHECK":
                return "local_check"
    result_path = directory / "verification-result.json"
    if result_path.is_file():
        with contextlib.suppress(OSError, core.OrchestratorError):
            suite = core.load_object(result_path).get("suite")
            if suite == "github-actions":
                return "github_actions"
            if suite == "github-pr-readiness":
                return "github_pull_request"
            if isinstance(suite, str):
                return "local_check"
    return None


def claim_check_owner(
    project_root: Path,
    *,
    operation_id: str,
    operation_type: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    """Atomically bind one legacy check-result directory to one operation type."""

    if operation_type not in CHECK_OWNER_TYPES:
        raise VerificationError(f"unsupported check owner type: {operation_type}")
    if not core.EVENT_ID_PATTERN.fullmatch(operation_id):
        raise VerificationError(f"invalid operation id: {operation_id!r}")
    directory = checks_root(project_root, state_dir=state_dir) / operation_id
    owner_path = directory / CHECK_OWNER_NAME
    if owner_path.is_file():
        try:
            owner = core.load_object(owner_path)
        except (OSError, core.OrchestratorError) as error:
            raise VerificationError(
                f"invalid check owner record: {owner_path}"
            ) from error
        if (
            not core.is_supported_schema_version(owner.get("schema_version"))
            or owner.get("kind") != CHECK_OWNER_KIND
            or owner.get("operation_id") != operation_id
        ):
            raise VerificationError(f"invalid check owner record: {owner_path}")
        if owner.get("operation_type") != operation_type:
            raise VerificationError(
                f"operation id {operation_id} is already owned by "
                f"{owner.get('operation_type')}"
            )
        return owner_path

    legacy_owner = _legacy_check_owner(directory)
    if legacy_owner is not None and legacy_owner != operation_type:
        raise VerificationError(
            f"operation id {operation_id} is already owned by {legacy_owner}"
        )
    if directory.exists() and legacy_owner is None and any(directory.iterdir()):
        raise VerificationError(
            f"operation id {operation_id} has unowned existing check artifacts"
        )
    owner = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": CHECK_OWNER_KIND,
        "operation_id": operation_id,
        "operation_type": operation_type,
        "created_at": core.utc_now(),
    }
    if core.claim_json(owner_path, owner):
        return owner_path
    try:
        existing = core.load_object(owner_path)
    except (OSError, core.OrchestratorError) as error:
        raise VerificationError(f"invalid check owner record: {owner_path}") from error
    if (
        existing.get("kind") == CHECK_OWNER_KIND
        and existing.get("operation_id") == operation_id
        and existing.get("operation_type") == operation_type
    ):
        return owner_path
    raise VerificationError(
        f"operation id {operation_id} was concurrently claimed by "
        f"{existing.get('operation_type')}"
    )


def checks_status(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    check_id: str | None = None,
    status: str | None = None,
    minimum_severity: str = "info",
    large_log_bytes: int = DEFAULT_LARGE_LOG_BYTES,
) -> dict[str, Any]:
    if large_log_bytes <= 0:
        raise VerificationError("large_log_bytes must be positive")
    project = project_root.expanduser().resolve()
    check_paths = selected_check_paths(project, state_dir=state_dir, check_id=check_id)

    summaries: dict[str, Any] = {}
    all_diagnostics: list[dict[str, str]] = []
    for result_path in check_paths:
        summary = summarize_check(
            project,
            result_path,
            large_log_bytes=large_log_bytes,
        )
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
            "large_log_bytes": large_log_bytes,
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


def summarize_check(
    project_root: Path,
    result_path: Path,
    *,
    large_log_bytes: int,
) -> dict[str, Any]:
    check_dir = result_path.parent
    directory_check_id = check_dir.name
    diagnostics: list[dict[str, str]] = []
    try:
        result = core.load_object(result_path)
    except (OSError, core.OrchestratorError) as error:
        active = active_check_summary(check_dir, result_path)
        if active is not None:
            return active
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
    artifact_sizes = verification_artifact_sizes(artifacts)
    commands = result.get("commands")
    if not isinstance(commands, list):
        commands = []
    command_summaries = [
        command_summary(project_root, command)
        for command in commands
        if isinstance(command, dict)
    ]
    for command in command_summaries:
        label = command.get("label") or "command"
        size = command.get("log_size")
        artifact_sizes[f"command:{label}"] = size if isinstance(size, int) else None
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
    diagnostics.extend(
        log_size_diagnostics(
            check_id=check_id,
            log_sizes=artifact_sizes,
            large_log_bytes=large_log_bytes,
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
            "log_sizes": artifact_sizes,
            "commands": command_summaries,
            "failed_commands": failed_commands,
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        }
    )
    return summary


def active_check_summary(check_dir: Path, result_path: Path) -> dict[str, Any] | None:
    if result_path.exists():
        return None
    descriptor_path = check_dir / "check.json"
    if not descriptor_path.is_file():
        return None
    try:
        descriptor = core.load_object(descriptor_path)
    except (OSError, core.OrchestratorError):
        return None
    status = descriptor.get("status")
    if descriptor.get("kind") != "ORCHESTRATOR_LOCAL_CHECK" or status not in {
        "starting",
        "running",
    }:
        return None
    summary = base_summary(
        directory_check_id=check_dir.name,
        check_id=str(descriptor.get("check_id") or check_dir.name),
        status=str(status),
        result_path=result_path,
        check_dir=check_dir,
        diagnostics=[],
    )
    summary.update(
        {
            "suite": descriptor.get("suite"),
            "execution": descriptor.get("execution"),
            "started_at": descriptor.get("started_at"),
            "descriptor_path": str(descriptor_path),
            "command_count": None,
            "failed_command_count": 0,
            "commands": [],
            "failed_commands": [],
            "artifacts": {"descriptor": str(descriptor_path)},
            "log_sizes": {},
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


def verification_artifact_sizes(artifacts: dict[str, Path]) -> dict[str, int | None]:
    sizes: dict[str, int | None] = {}
    for name, path in artifacts.items():
        if not name.endswith("log") and name != "full_log":
            continue
        if not path.is_file():
            sizes[name] = None
            continue
        try:
            sizes[name] = path.stat().st_size
        except OSError:
            sizes[name] = None
    return sizes


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
    log_size = None
    if log_path is not None and log_path.is_file():
        try:
            log_size = log_path.stat().st_size
        except OSError:
            log_size = None
    return {
        "label": command.get("label"),
        "required": command.get("required", True),
        "status": command.get("status"),
        "exit_code": command.get("exit_code"),
        "duration_seconds": command.get("duration_seconds"),
        "command": command.get("command"),
        "log_path": str(log_path) if log_path is not None else None,
        "log_size": log_size,
        "output_line_count": command.get("output_line_count"),
        "error": command.get("error"),
    }


def log_size_diagnostics(
    *,
    check_id: str,
    log_sizes: dict[str, int | None],
    large_log_bytes: int,
) -> list[dict[str, str]]:
    large = {
        name: size
        for name, size in log_sizes.items()
        if isinstance(size, int) and size > large_log_bytes
    }
    if not large:
        return []
    details = ", ".join(f"{name}={size} bytes" for name, size in sorted(large.items()))
    return [
        diagnostic(
            code="verification_large_log",
            severity="info",
            message=(
                f"verification check {check_id} has large log artifacts "
                f"above {large_log_bytes} bytes: {details}"
            ),
            suggested_action=(
                "Read verification-result.json and summary.txt first. Inspect "
                "failed command logs or targeted tails instead of pasting full "
                "logs into host chats or reports."
            ),
        )
    ]


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
