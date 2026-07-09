"""Read-only project health diagnostics."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
from typing import Any

from . import __version__, binding, claude_stream, core, watcher, workers

DOCTOR_KIND = "ORCHESTRATOR_DOCTOR_REPORT"
CHECK_STATUSES = {"ok", "warn", "error", "skipped"}


class DiagnosticsError(RuntimeError):
    """A deterministic diagnostics failure."""


def check(
    name: str,
    title: str,
    status: str,
    detail: str,
    *,
    hint: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in CHECK_STATUSES:
        raise DiagnosticsError(f"unsupported doctor check status: {status}")
    return {
        "name": name,
        "title": title,
        "status": status,
        "detail": detail,
        "hint": hint,
        "data": data or {},
    }


def aggregate_status(checks: list[dict[str, Any]]) -> str:
    statuses = {item.get("status") for item in checks}
    if "error" in statuses:
        return "error"
    if "warn" in statuses:
        return "warn"
    return "ok"


def doctor_exit_code(report: dict[str, Any], *, strict: bool = False) -> int:
    status = report.get("status")
    if status == "error" or (strict and status == "warn"):
        return 2
    return 0


def run_doctor(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    host: str | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    if not project.is_dir():
        raise DiagnosticsError(f"project root is not a directory: {project}")
    checks = [
        check_state_layout(project, state_dir=state_dir),
        check_schema_compatibility(project, state_dir=state_dir),
        check_binding(project, state_dir=state_dir),
        check_workers(project, state_dir=state_dir),
        check_watcher_channel(project, state_dir=state_dir, host=host),
        check_engine_import(),
    ]
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": DOCTOR_KIND,
        "project_root": str(project),
        "state_dir": state_dir,
        "engine_version": __version__,
        "supported_schema_versions": sorted(core.SUPPORTED_SCHEMA_VERSIONS),
        "status": aggregate_status(checks),
        "checks": checks,
        "generated_at": core.utc_now(),
    }


def check_state_layout(project: Path, *, state_dir: str) -> dict[str, Any]:
    state = core.state_root(project, state_dir=state_dir)
    paths = {
        "state_root": state,
        "events": core.events_root(project, state_dir=state_dir),
        "inbox": core.inbox_root(project, state_dir=state_dir),
        "signals": core.inbox_root(project, state_dir=state_dir) / "signals",
    }
    missing = [name for name, path in paths.items() if not path.is_dir()]
    if missing:
        return check(
            "state_layout",
            "Orchestrator state layout exists",
            "warn",
            "state layout is incomplete",
            hint="Run `orchestrator-engine adopt` for this project.",
            data={
                "missing": missing,
                "paths": {name: str(path) for name, path in paths.items()},
            },
        )
    writable = os.access(state, os.W_OK)
    return check(
        "state_layout",
        "Orchestrator state layout exists",
        "ok" if writable else "warn",
        "state layout exists" if writable else "state layout is not writable",
        hint=None if writable else "Fix permissions before dispatching workers.",
        data={"paths": {name: str(path) for name, path in paths.items()}},
    )


def check_schema_compatibility(project: Path, *, state_dir: str) -> dict[str, Any]:
    survey = core.survey_schema_versions(project, state_dir=state_dir)
    incompatible = [
        item
        for item in survey["unsupported"]
        if type(item.get("schema_version")) is int
    ]
    malformed = [
        item for item in survey["unsupported"] if item not in incompatible
    ]
    if incompatible:
        return check(
            "schema_compatibility",
            "Durable artifacts use supported schemas",
            "error",
            "unsupported schema_version found in durable artifacts",
            hint="Install an OrchestratorEngine version that supports these schemas.",
            data={**survey, "incompatible": incompatible, "malformed": malformed},
        )
    if malformed or survey["unreadable_count"]:
        return check(
            "schema_compatibility",
            "Durable artifacts use supported schemas",
            "warn",
            "some durable artifacts have malformed or unreadable schema metadata",
            hint="Inspect reported files manually; doctor does not delete them.",
            data={**survey, "incompatible": incompatible, "malformed": malformed},
        )
    return check(
        "schema_compatibility",
        "Durable artifacts use supported schemas",
        "ok",
        f"{survey['supported_count']} durable document(s) at supported schemas",
        data=survey,
    )


def check_binding(project: Path, *, state_dir: str) -> dict[str, Any]:
    try:
        bound = binding.load_binding(project, state_dir=state_dir)
    except (OSError, RuntimeError, ValueError) as error:
        return check(
            "binding",
            "Host binding is valid",
            "error",
            f"binding is invalid: {error}",
            hint="Recreate the binding from the host chat.",
            data={
                "binding_path": str(
                    binding.binding_path(project, state_dir=state_dir)
                )
            },
        )
    if bound is None:
        return check(
            "binding",
            "Host binding is valid",
            "warn",
            "no host binding configured",
            hint="Run `orchestrator-engine bind --host ...` from the host chat.",
            data={
                "binding_path": str(
                    binding.binding_path(project, state_dir=state_dir)
                )
            },
        )
    return check(
        "binding",
        "Host binding is valid",
        "ok",
        f"bound to host {bound['host']}",
        data={
            "host": bound["host"],
            "target_thread_id": bound.get("target_thread_id"),
            "binding_path": bound.get("binding_path"),
        },
    )


def check_workers(project: Path, *, state_dir: str) -> dict[str, Any]:
    try:
        registry = workers.load_registry(project, state_dir=state_dir)
    except (OSError, RuntimeError, ValueError) as error:
        return check(
            "workers",
            "Worker registry is usable",
            "error",
            f"worker registry is invalid: {error}",
            hint="Fix `.orchestrator/workers.toml`.",
            data={
                "config_path": str(
                    workers.workers_config_path(project, state_dir=state_dir)
                )
            },
        )
    enabled = [name for name, config in registry.items() if config["enabled"]]
    warnings = [
        {"worker": name, **warning}
        for name, config in sorted(registry.items())
        for warning in config.get("warnings", [])
    ]
    if not registry:
        return check(
            "workers",
            "Worker registry is usable",
            "warn",
            "no workers configured",
            hint="Create `.orchestrator/workers.toml` or run `adopt`.",
            data={
                "config_path": str(
                    workers.workers_config_path(project, state_dir=state_dir)
                ),
                "worker_count": 0,
                "enabled_count": 0,
            },
        )
    status = "warn" if warnings or not enabled else "ok"
    detail = (
        f"{len(enabled)} enabled worker(s)"
        if enabled
        else "worker registry exists but no workers are enabled"
    )
    return check(
        "workers",
        "Worker registry is usable",
        status,
        detail,
        hint=(
            "Enable at least one worker profile."
            if not enabled
            else "Inspect worker warnings before detached dispatch."
            if warnings
            else None
        ),
        data={
            "config_path": str(
                workers.workers_config_path(project, state_dir=state_dir)
            ),
            "worker_count": len(registry),
            "enabled_count": len(enabled),
            "warnings": warnings,
        },
    )


def check_watcher_channel(
    project: Path,
    *,
    state_dir: str,
    host: str | None,
) -> dict[str, Any]:
    bound = None
    binding_error = None
    try:
        bound = binding.load_binding(project, state_dir=state_dir)
    except (OSError, RuntimeError, ValueError) as error:
        binding_error = str(error)
    selected_host = host or (bound.get("host") if bound else None)
    if selected_host is None:
        return check(
            "watcher_channel",
            "Wake channel matches the bound host",
            "skipped",
            "no host selected and no binding configured",
            hint="Bind a host or pass `doctor --host HOST`.",
            data={"binding_error": binding_error},
        )
    if selected_host == "claude":
        return check_claude_stream(project, state_dir=state_dir)
    if selected_host not in watcher.HOST_ADAPTERS:
        return check(
            "watcher_channel",
            "Wake channel matches the bound host",
            "error",
            f"host {selected_host} has no callback adapter",
            hint="Use the host's documented wake mechanism.",
            data={"host": selected_host},
        )
    status = watcher.service_status([project], state_dir=state_dir, host=selected_host)
    if status["status"] in {"running"} and not status.get("warnings"):
        severity = "ok"
    elif status["status"] in {"crashed", "degraded"}:
        severity = "error"
    else:
        severity = "warn"
    return check(
        "watcher_channel",
        "Wake channel matches the bound host",
        severity,
        f"{selected_host} callback service is {status['status']}",
        hint=(
            None
            if severity == "ok"
            else "Start or repair the host-scoped watcher service."
        ),
        data={"host": selected_host, "service_status": status},
    )


def check_claude_stream(project: Path, *, state_dir: str) -> dict[str, Any]:
    status = claude_stream.stream_status([project], state_dir=state_dir)
    severity = "ok" if status["status"] == "fresh" else "warn"
    if status["status"] == "erroring":
        severity = "error"
    return check(
        "watcher_channel",
        "Wake channel matches the bound host",
        severity,
        f"claude stream is {status['status']}",
        hint=(
            None
            if severity == "ok"
            else "Arm `watcher stream` from the Claude host chat."
        ),
        data={"host": "claude", "stream_status": status},
    )


def check_engine_import() -> dict[str, Any]:
    try:
        installed = importlib.metadata.version("orchestrator-engine")
    except importlib.metadata.PackageNotFoundError:
        return check(
            "engine_import",
            "Engine package is installed for re-exec",
            "warn",
            "orchestrator-engine is not installed as a package",
            hint=(
                "Install the package in this environment; detached supervisors "
                "re-exec `python -m orchestrator_engine.cli`."
            ),
        )
    return check(
        "engine_import",
        "Engine package is installed for re-exec",
        "ok",
        f"installed package version {installed}",
        data={"installed_version": installed},
    )
