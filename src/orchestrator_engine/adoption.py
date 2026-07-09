"""Idempotent project adoption scaffolding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import binding, core, workers

ADOPTION_KIND = "ORCHESTRATOR_ADOPTION"

WORKERS_TEMPLATE = """# OrchestratorEngine worker profiles.
#
# Enable and edit only the profiles available on this machine. The engine
# executes command arrays exactly as data; model names and permission flags are
# provider-specific and belong in this adopting project file.

[workers.example]
enabled = false
command = ["python", "-c", "import sys; sys.stdin.read(); print('ok')"]
prompt_via = "stdin"
"""


class AdoptionError(RuntimeError):
    """A deterministic adoption failure."""


def adopt_project(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    host: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    if not project.is_dir():
        raise AdoptionError(f"project root is not a directory: {project}")
    if project == Path(project.anchor).resolve() or project == project.home():
        raise AdoptionError(f"refusing to adopt unsafe project root: {project}")
    validate_state_dir(state_dir)
    if host is not None and host not in binding.SUPPORTED_HOSTS:
        raise AdoptionError(f"unsupported host: {host}")

    directories = [
        core.state_root(project, state_dir=state_dir),
        core.events_root(project, state_dir=state_dir),
        core.inbox_root(project, state_dir=state_dir),
        core.inbox_root(project, state_dir=state_dir) / "signals",
        core.inbox_root(project, state_dir=state_dir) / "logs",
        core.inbox_root(project, state_dir=state_dir) / "notifications",
        core.inbox_root(project, state_dir=state_dir) / "thread-wakeups",
        workers.tasks_root(project, state_dir=state_dir),
        core.state_root(project, state_dir=state_dir) / "prompts",
    ]
    workers_config = workers.workers_config_path(project, state_dir=state_dir)
    created: list[str] = []
    skipped: list[str] = []

    for directory in directories:
        if directory.exists():
            skipped.append(state_relative(project, directory))
            continue
        created.append(state_relative(project, directory))
        if not dry_run:
            directory.mkdir(parents=True, exist_ok=True)

    if workers_config.exists():
        skipped.append(state_relative(project, workers_config))
    else:
        created.append(state_relative(project, workers_config))
        if not dry_run:
            workers_config.parent.mkdir(parents=True, exist_ok=True)
            workers_config.write_text(WORKERS_TEMPLATE, encoding="utf-8")

    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": ADOPTION_KIND,
        "project_root": str(project),
        "state_dir": state_dir,
        "status": "created" if created else "already_present",
        "created": created,
        "skipped": skipped,
        "dry_run": dry_run,
        "next_steps": next_steps(project, host=host),
    }


def state_relative(project: Path, path: Path) -> str:
    return str(path.relative_to(project))


def validate_state_dir(state_dir: str) -> None:
    path = Path(state_dir)
    if not state_dir or path.is_absolute() or ".." in path.parts:
        raise AdoptionError(
            "state_dir must be a non-empty relative path inside the project"
        )


def next_steps(project: Path, *, host: str | None) -> list[str]:
    root = str(project)
    selected = host or "HOST"
    steps = [
        f"orchestrator-engine --project-root {root} bind --host {selected}",
        f"edit {core.DEFAULT_STATE_DIR}/workers.toml and enable installed workers",
        f"orchestrator-engine --project-root {root} worker list",
    ]
    if host == "claude":
        steps.append(
            f"orchestrator-engine --project-root {root} watcher stream"
        )
    elif host in {"codex", "vscode"}:
        steps.append(
            f"orchestrator-engine --project-root {root} watcher "
            f"--host {host} --action callback service start"
        )
    else:
        steps.append("bind a host, then start its documented wake channel")
    steps.append(f"orchestrator-engine --project-root {root} doctor")
    return steps
