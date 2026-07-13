"""Read-only adopter upgrade readiness report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import __version__, core, diagnostics, worker_diagnostics, workers

UPGRADE_CHECK_KIND = "ORCHESTRATOR_UPGRADE_CHECK"
UPGRADE_STATUSES = {"ready", "review_required", "blocked"}
MAX_NEXT_ACTIONS = 32


def run_upgrade_check(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    host: str | None = None,
) -> dict[str, Any]:
    """Summarize adopter readiness without rewriting policy or durable state."""
    project = project_root.expanduser().resolve()
    doctor = diagnostics.run_doctor(project, state_dir=state_dir, host=host)
    try:
        worker_report = workers.diagnose_workers(
            project,
            state_dir=state_dir,
            minimum_severity="info",
            enabled_only=True,
        )
    except (OSError, RuntimeError, ValueError) as error:
        worker_report = failed_worker_report(project, state_dir, error)
    doctor_checks = [
        {
            key: item.get(key)
            for key in ("name", "status", "detail", "hint")
            if item.get(key) is not None
        }
        for item in doctor["checks"]
    ]
    worker_diagnostics = [
        {"worker": name, **item}
        for name, summary in worker_report["workers"].items()
        for item in summary["diagnostics"]
        if item.get("severity") in {"warning", "error"}
    ]
    policy_diagnostics = [
        dict(item) for item in worker_report.get("policy_diagnostics", [])
    ]
    policy_updates = [
        name
        for name, status in worker_report.get("policies", {}).items()
        if status.get("status") == "different"
    ]

    has_error = doctor["status"] == "error" or any(
        item.get("severity") == "error"
        for item in [*worker_diagnostics, *policy_diagnostics]
    )
    has_review = (
        doctor["status"] == "warn"
        or bool(worker_diagnostics)
        or bool(policy_diagnostics)
        or bool(policy_updates)
    )
    status = "blocked" if has_error else "review_required" if has_review else "ready"

    actions: list[dict[str, str]] = []
    for item in doctor_checks:
        if item.get("status") not in {"warn", "error"}:
            continue
        add_action(
            actions,
            code=f"doctor:{item['name']}",
            action=str(item.get("hint") or item.get("detail") or "Inspect the check."),
        )
    for item in [*worker_diagnostics, *policy_diagnostics]:
        add_action(
            actions,
            code=str(item.get("code") or "worker_diagnostic"),
            action=str(item.get("suggested_action") or "Inspect worker diagnostics."),
        )
    if worker_report["dispatch"]["intent_enforcement"] != "strict":
        add_action(
            actions,
            code="consider_strict_intent",
            action=(
                "Consider strict intent enforcement for AI profiles and include "
                "intent.verification on every AI dispatch."
            ),
        )

    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": UPGRADE_CHECK_KIND,
        "project_root": str(project),
        "state_dir": state_dir,
        "engine_version": __version__,
        "supported_schema_versions": sorted(core.SUPPORTED_SCHEMA_VERSIONS),
        "status": status,
        "doctor": {
            "status": doctor["status"],
            "checks": doctor_checks,
        },
        "workers": {
            "enabled_count": worker_report["worker_count"],
            "worst_severity": worker_report["worst_severity"],
            "diagnostics": worker_diagnostics,
            "policy_diagnostics": policy_diagnostics,
            "policy_updates": policy_updates,
        },
        "dispatch": worker_report["dispatch"],
        "next_actions": actions,
        "manual_checks": [
            {
                "code": "reusable_prompt_audit",
                "action": (
                    "Review AGENTS.md, CLAUDE.md, Copilot instructions and reusable "
                    "prompt templates for unconditional test commands. Do not edit "
                    "historical task artifacts."
                ),
            },
            {
                "code": "smoke_dispatch",
                "action": (
                    "Dispatch one harmless task with intent.verification and inspect "
                    "task.json, effective-prompt.md and evidence.json."
                ),
            },
        ],
        "generated_at": core.utc_now(),
    }


def add_action(
    actions: list[dict[str, str]], *, code: str, action: str
) -> None:
    if len(actions) >= MAX_NEXT_ACTIONS or any(
        item["code"] == code for item in actions
    ):
        return
    actions.append({"code": code, "action": action})


def failed_worker_report(
    project: Path,
    state_dir: str,
    error: Exception,
) -> dict[str, Any]:
    """Represent an unreadable worker registry as bounded diagnostic data."""
    detail = str(error).replace("\n", " ")[:500]
    item = worker_diagnostics.diagnostic(
        code="worker_registry_unreadable",
        severity="error",
        message=f"worker registry could not be diagnosed: {detail}",
        suggested_action="Fix workers.toml, then run upgrade check again.",
    )
    return {
        "worker_count": 0,
        "worst_severity": "error",
        "workers": {"<registry>": {"diagnostics": [item]}},
        "policy_diagnostics": [],
        "policies": {},
        "dispatch": {
            "availability_mode": "off",
            "intent_enforcement": "off",
            "source": str(workers.workers_config_path(project, state_dir=state_dir)),
            "readable": False,
        },
    }


def exit_code(report: dict[str, Any], *, strict: bool = False) -> int:
    status = report.get("status")
    if status not in UPGRADE_STATUSES:
        raise ValueError(f"unsupported upgrade status: {status}")
    if status == "blocked" or (strict and status == "review_required"):
        return 2
    return 0
