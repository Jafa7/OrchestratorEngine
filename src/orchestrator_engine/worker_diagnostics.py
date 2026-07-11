"""Advisory diagnostics for detached worker profiles."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PureWindowsPath
from typing import Any

SEVERITIES = ("info", "warning", "error")
SEVERITY_RANK = {severity: index for index, severity in enumerate(SEVERITIES)}
RATE_LIMIT_SCAN_BYTES = 32 * 1024
RATE_LIMIT_PATTERNS = (
    re.compile(r"you(?:'|\u2019)ve hit your session limit", re.IGNORECASE),
    re.compile(r"\brate[ _-]limit(?:ed|[ _-]exceeded)\b", re.IGNORECASE),
    re.compile(r"\busage[ _-]limit(?:ed|[ _-]exceeded)\b", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"\b(?:http\s*(?:status\s*)?)?429\b", re.IGNORECASE),
    re.compile(r"insufficient quota", re.IGNORECASE),
)


class WorkerDiagnosticError(RuntimeError):
    """A deterministic worker diagnostic failure."""


def diagnostic(
    *,
    code: str,
    severity: str,
    message: str,
    suggested_action: str,
) -> dict[str, str]:
    if severity not in SEVERITY_RANK:
        raise WorkerDiagnosticError(f"unsupported diagnostic severity: {severity}")
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "suggested_action": suggested_action,
    }


def evaluate_profile(
    *,
    name: str,
    command: list[str],
    prompt_via: str,
    timeout_seconds: int | float | None,
    expect_long_running: bool = False,
    availability_probe: list[str] | None = None,
    availability_timeout_seconds: int | float | None = None,
) -> list[dict[str, str]]:
    """Return non-blocking diagnostics for one worker profile."""

    diagnostics: list[dict[str, str]] = []
    if availability_probe is not None and availability_timeout_seconds is None:
        diagnostics.append(diagnostic(
            code="availability_probe_timeout_absent", severity="error",
            message=f"worker {name} availability probe has no timeout",
            suggested_action="Set a finite availability_timeout_seconds.",
        ))
    if command:
        executable = executable_name(command[0])
        flags = set(command[1:])
        command_text = " ".join(command[1:])
        diagnostics.extend(
            provider_diagnostics(
                name=name,
                executable=executable,
                flags=flags,
                command_text=command_text,
                prompt_via=prompt_via,
            )
        )
    if timeout_seconds is None and not expect_long_running:
        diagnostics.append(
            diagnostic(
                code="worker_timeout_absent",
                severity="info",
                message=(
                    f"worker {name} has no timeout_seconds; this is acceptable "
                    "for long-running tasks but quick profiles may run forever"
                ),
                suggested_action=(
                    "Set timeout_seconds for bounded quick/check profiles, or "
                    "set expect_long_running = true to mark the omission as "
                    "intentional."
                ),
            )
        )
    return diagnostics


def output_metadata(value: bytes | str | None) -> dict[str, Any]:
    """Describe probe output without exposing potentially sensitive content."""

    raw = value if isinstance(value, bytes) else (value or "").encode("utf-8")
    return {
        "output_bytes": len(raw),
        "output_sha256": hashlib.sha256(raw).hexdigest(),
    }


def run_availability_probe(config: dict[str, Any]) -> dict[str, Any]:
    command = config.get("availability_probe")
    if command is None:
        return {"status": "not_configured"}
    try:
        completed = subprocess.run(
            command, capture_output=True, check=False,
            timeout=float(config["availability_timeout_seconds"]),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "status": "probe_error",
            "error": "probe timed out",
            **output_metadata(error.stdout or error.stderr),
        }
    except OSError as error:
        return {"status": "probe_error", "error": type(error).__name__}
    output = completed.stdout or completed.stderr
    return {
        "status": "available" if completed.returncode == 0 else "unavailable",
        "exit_code": completed.returncode,
        **output_metadata(output),
    }


def classify_rate_limit(stdout_path: Path, stderr_path: Path) -> bool:
    chunks = []
    for path in (stdout_path, stderr_path):
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                start = max(handle.tell() - RATE_LIMIT_SCAN_BYTES, 0)
                handle.seek(start)
                chunks.append(
                    handle.read(RATE_LIMIT_SCAN_BYTES).decode("utf-8", errors="replace")
                )
        except OSError:
            continue
    text = "\n".join(chunks)
    return any(pattern.search(text) for pattern in RATE_LIMIT_PATTERNS)


def executable_name(command: str) -> str:
    if "\\" in command or PureWindowsPath(command).drive:
        return PureWindowsPath(command).name.lower()
    return Path(command).name.lower()


def provider_diagnostics(
    *,
    name: str,
    executable: str,
    flags: set[str],
    command_text: str,
    prompt_via: str,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if executable in {"copilot", "copilot.exe"} and (
        "--allow-all" not in flags or "--no-ask-user" not in flags
    ):
        diagnostics.append(
            diagnostic(
                code="copilot_may_request_approval",
                severity="warning",
                message=(
                    f"worker {name} runs Copilot detached with prompt_via="
                    f"{prompt_via!r} but does not include both --allow-all "
                    "and --no-ask-user; it may stall on approval prompts"
                ),
                suggested_action=(
                    "Add both --allow-all and --no-ask-user, or replace this "
                    "profile with an explicitly non-interactive Copilot command."
                ),
            )
        )
    if executable in {"codex", "codex.exe"} and "exec" in flags:
        bypasses_approvals_and_sandbox = (
            "--dangerously-bypass-approvals-and-sandbox" in flags
        )
        if not bypasses_approvals_and_sandbox and (
            "approval_policy" not in command_text or "never" not in command_text
        ):
            diagnostics.append(
                diagnostic(
                    code="codex_may_request_approval",
                    severity="warning",
                    message=(
                        f"worker {name} runs codex exec detached but does not "
                        "set approval_policy=\"never\" in the command or "
                        "otherwise declare a non-interactive approval policy"
                    ),
                    suggested_action=(
                        "Add `-c approval_policy=\"never\"` or another "
                        "documented non-interactive approval policy."
                    ),
                )
            )
        has_sandbox_strategy = (
            "sandbox_mode" in command_text or "--sandbox" in flags or "-s" in flags
        )
        if not bypasses_approvals_and_sandbox and not has_sandbox_strategy:
            diagnostics.append(
                diagnostic(
                    code="codex_missing_sandbox_strategy",
                    severity="warning",
                    message=(
                        f"worker {name} runs codex exec detached without an "
                        "explicit sandbox_mode override; ensure the selected "
                        "Codex config is intentional for this worker profile"
                    ),
                    suggested_action=(
                        "Set `--sandbox ...` or an explicit "
                        "`-c sandbox_mode=\"...\"` appropriate for this "
                        "detached worker profile."
                    ),
                )
            )
    if (
        executable in {"claude", "claude.exe"}
        and "-p" in flags
        and "--permission-mode" not in flags
        and "--dangerously-skip-permissions" not in flags
    ):
        diagnostics.append(
            diagnostic(
                code="claude_missing_permission_mode",
                severity="warning",
                message=(
                    f"worker {name} runs claude -p detached without an "
                    "explicit --permission-mode; it may stall or refuse "
                    "tool use if the default mode is interactive"
                ),
                suggested_action=(
                    "Add an explicit --permission-mode value that matches this "
                    "profile's intended autonomy, or use "
                    "--dangerously-skip-permissions only inside an appropriate "
                    "external security boundary."
                ),
            )
        )
    return diagnostics


def filter_diagnostics(
    diagnostics: list[dict[str, str]],
    *,
    minimum_severity: str = "info",
) -> list[dict[str, str]]:
    if minimum_severity not in SEVERITY_RANK:
        raise WorkerDiagnosticError(
            f"unsupported diagnostic severity: {minimum_severity}"
        )
    minimum = SEVERITY_RANK[minimum_severity]
    return [
        item
        for item in diagnostics
        if SEVERITY_RANK.get(item.get("severity", ""), -1) >= minimum
    ]


def severity_counts(diagnostics: list[dict[str, str]]) -> dict[str, int]:
    return {
        severity: sum(
            1 for item in diagnostics if item.get("severity") == severity
        )
        for severity in SEVERITIES
    }


def worst_severity(diagnostics: list[dict[str, str]]) -> str | None:
    worst = None
    for item in diagnostics:
        severity = item.get("severity")
        if severity not in SEVERITY_RANK:
            continue
        if worst is None or SEVERITY_RANK[severity] > SEVERITY_RANK[worst]:
            worst = severity
    return worst


def exit_code_for_worst(worst: str | None) -> int:
    if worst == "error":
        return 3
    if worst == "warning":
        return 2
    return 0


def profile_summary(
    *,
    name: str,
    config: dict[str, Any],
    diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "name": name,
        "enabled": config["enabled"],
        "command": config["command"],
        "prompt_via": config["prompt_via"],
        "timeout_seconds": config["timeout_seconds"],
        "expect_long_running": config["expect_long_running"],
        "metadata": config["extras"],
        "diagnostic_count": len(diagnostics),
        "severity_counts": severity_counts(diagnostics),
        "worst_severity": worst_severity(diagnostics),
        "diagnostics": diagnostics,
    }
