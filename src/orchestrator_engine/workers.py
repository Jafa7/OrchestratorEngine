"""CLI worker registry and detached runner.

The host chat dispatches a task with `worker run`, which returns immediately so
the chat turn can end. A detached supervisor process runs the worker CLI,
captures its output, and emits the standard terminal event + inbox signal on
exit — which is what wakes the host chat again.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from . import binding, core

WORKERS_CONFIG_NAME = "workers.toml"
PROMPT_MODES = {"arg", "stdin"}
TASK_KIND = "WORKER_TASK"
RESERVED_KEYS = {"enabled", "command", "prompt_via", "timeout_seconds"}
# Workers may legitimately run for hours with no configured timeout; the
# supervisor refreshes the task descriptor on this cadence so long tasks stay
# observable (`last_alive_at`) instead of looking stuck.
TASK_HEARTBEAT_INTERVAL_SECONDS = 30.0


class WorkerError(RuntimeError):
    """A deterministic worker registry or runner failure."""


def workers_config_path(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / WORKERS_CONFIG_NAME


def tasks_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "tasks"


def task_dir_for(
    project_root: Path,
    task_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    if not task_id or "/" in task_id or "\\" in task_id or task_id.startswith("."):
        raise WorkerError(f"invalid task id: {task_id!r}")
    return tasks_root(project_root, state_dir=state_dir) / task_id


def load_registry(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, dict[str, Any]]:
    path = workers_config_path(project_root, state_dir=state_dir)
    if not path.is_file():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise WorkerError(f"invalid workers config: {path}: {error}") from error
    workers = value.get("workers")
    if not isinstance(workers, dict):
        raise WorkerError(f"workers config must contain a [workers.*] table: {path}")
    registry: dict[str, dict[str, Any]] = {}
    for name, config in workers.items():
        registry[name] = validate_worker_config(name, config)
    return registry


def validate_worker_config(name: str, config: object) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise WorkerError(f"worker {name} config must be a table")
    command = config.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise WorkerError(f"worker {name} requires a non-empty command list")
    prompt_via = config.get("prompt_via", "arg")
    if prompt_via not in PROMPT_MODES:
        raise WorkerError(f"worker {name} has unsupported prompt_via: {prompt_via}")
    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise WorkerError(f"worker {name} enabled flag must be a boolean")
    timeout_seconds = config.get("timeout_seconds")
    if timeout_seconds is not None and (
        not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0
    ):
        raise WorkerError(f"worker {name} timeout_seconds must be positive")
    extras = {
        key: value
        for key, value in config.items()
        if key not in RESERVED_KEYS
    }
    return {
        "name": name,
        "enabled": enabled,
        "command": list(command),
        "prompt_via": prompt_via,
        "timeout_seconds": timeout_seconds,
        "extras": extras,
    }


def require_worker(
    project_root: Path,
    worker: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    registry = load_registry(project_root, state_dir=state_dir)
    if not registry:
        raise WorkerError(
            "no workers configured; create "
            f"{workers_config_path(project_root, state_dir=state_dir)}"
        )
    config = registry.get(worker)
    if config is None:
        raise WorkerError(
            f"unknown worker: {worker}; configured: {', '.join(sorted(registry))}"
        )
    if not config["enabled"]:
        raise WorkerError(f"worker {worker} is disabled")
    return config


def list_workers(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    registry = load_registry(project_root, state_dir=state_dir)
    return {
        "schema_version": core.SCHEMA_VERSION,
        "config_path": str(workers_config_path(project_root, state_dir=state_dir)),
        "workers": {
            name: {
                "enabled": config["enabled"],
                "command": config["command"],
                "prompt_via": config["prompt_via"],
                "timeout_seconds": config["timeout_seconds"],
                **config["extras"],
            }
            for name, config in sorted(registry.items())
        },
    }


def run_worker(
    project_root: Path,
    *,
    worker: str,
    task_id: str,
    prompt_file: Path,
    state_dir: str = core.DEFAULT_STATE_DIR,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    """Spawn a detached supervisor for the worker and return immediately."""
    project = project_root.expanduser().resolve()
    require_worker(project, worker, state_dir=state_dir)
    prompt = core.ensure_file(prompt_file, field="prompt")
    task_dir = task_dir_for(project, task_id, state_dir=state_dir)
    descriptor_path = task_dir / "task.json"
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive create claims the task id atomically (no check-then-act
        # race between concurrent dispatches).
        with descriptor_path.open("x", encoding="utf-8") as handle:
            handle.write("{}\n")
    except FileExistsError:
        raise WorkerError(f"task already exists: {descriptor_path}") from None
    supervisor_log = task_dir / "supervisor.log"
    # Snapshot the dispatching chat BEFORE spawning: the supervisor reads
    # wake_target from task.json, so it must be durable before the child can
    # possibly look for it.
    wake_target = capture_wake_target(project, state_dir=state_dir)
    descriptor = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": TASK_KIND,
        "task_id": task_id,
        "worker": worker,
        "status": "starting",
        "prompt_file": str(prompt),
        "task_dir": str(task_dir),
        "supervisor_log": str(supervisor_log),
        "created_at": core.utc_now(),
    }
    if wake_target is not None:
        descriptor["wake_target"] = wake_target
    core.atomic_json(descriptor_path, descriptor)
    command = [
        sys.executable,
        "-m",
        "orchestrator_engine.cli",
        "--project-root",
        str(project),
        "--state-dir",
        state_dir,
        "worker",
        "supervise",
        "--worker",
        worker,
        "--task-id",
        task_id,
        "--prompt-file",
        str(prompt),
    ]
    with supervisor_log.open("ab") as log:
        process = popen_factory(
            command,
            cwd=str(project),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    # Merge instead of overwrite: a very fast worker may already have
    # finished and finalized the descriptor between spawn and this write.
    with contextlib.suppress(OSError, core.OrchestratorError):
        descriptor.update(core.load_object(descriptor_path))
    if descriptor.get("status") == "starting":
        descriptor["status"] = "running"
    descriptor["supervisor_pid"] = int(process.pid)
    core.atomic_json(descriptor_path, descriptor)
    return {**descriptor, "descriptor_path": str(descriptor_path)}


def capture_wake_target(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any] | None:
    bound = binding.load_binding(project_root, state_dir=state_dir)
    if bound is None:
        return None
    return binding.wake_target_from_binding(bound)


def touch_descriptor(task_dir: Path, updates: dict[str, Any]) -> None:
    descriptor_path = task_dir / "task.json"
    try:
        descriptor = core.load_object(descriptor_path)
    except (OSError, core.OrchestratorError):
        descriptor = {}
    descriptor.update(updates)
    core.atomic_json(descriptor_path, descriptor)


def supervise_worker(
    project_root: Path,
    *,
    worker: str,
    task_id: str,
    prompt_file: Path,
    state_dir: str = core.DEFAULT_STATE_DIR,
    popen_factory=subprocess.Popen,
    heartbeat_interval_seconds: float = TASK_HEARTBEAT_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Run the worker CLI to completion and emit the terminal event."""
    project = project_root.expanduser().resolve()
    config = require_worker(project, worker, state_dir=state_dir)
    prompt = core.ensure_file(prompt_file, field="prompt")
    prompt_text = prompt.read_text(encoding="utf-8")
    task_dir = task_dir_for(project, task_id, state_dir=state_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    wake_target: dict[str, Any] | None = None
    descriptor_path = task_dir / "task.json"
    if descriptor_path.exists():
        with contextlib.suppress(OSError, core.OrchestratorError, binding.BindingError):
            descriptor_snapshot = core.load_object(descriptor_path)
            maybe_target = descriptor_snapshot.get("wake_target")
            if isinstance(maybe_target, dict):
                binding.validate_wake_target(maybe_target)
                wake_target = maybe_target
    stdout_path = task_dir / "worker-stdout.log"
    stderr_path = task_dir / "worker-stderr.log"

    command = list(config["command"])
    stdin_payload: str | None = None
    if config["prompt_via"] == "arg":
        command.append(prompt_text)
    else:
        stdin_payload = prompt_text

    started_at = core.utc_now()
    start = time.monotonic()
    terminal_status = "completed"
    exit_code: int | None = None
    failure_reason: str | None = None
    try:
        with (
            stdout_path.open("wb") as stdout,
            stderr_path.open("wb") as stderr,
        ):
            process = popen_factory(
                command,
                cwd=str(project),
                stdin=subprocess.PIPE if stdin_payload is not None else (
                    subprocess.DEVNULL
                ),
                stdout=stdout,
                stderr=stderr,
            )
            if stdin_payload is not None:
                assert process.stdin is not None
                process.stdin.write(stdin_payload.encode("utf-8"))
                process.stdin.close()
            timeout_seconds = config["timeout_seconds"]
            deadline = (
                start + float(timeout_seconds)
                if timeout_seconds is not None
                else None
            )
            while True:
                poll_timeout = heartbeat_interval_seconds
                if deadline is not None:
                    poll_timeout = min(
                        poll_timeout,
                        max(deadline - time.monotonic(), 0.1),
                    )
                try:
                    exit_code = process.wait(timeout=poll_timeout)
                    break
                except subprocess.TimeoutExpired:
                    if deadline is not None and time.monotonic() >= deadline:
                        process.kill()
                        process.wait()
                        terminal_status = "timed_out"
                        failure_reason = (
                            f"worker exceeded {timeout_seconds} seconds"
                        )
                        break
                    # No timeout configured means the worker may run for
                    # hours; refresh the descriptor so it stays observable.
                    touch_descriptor(
                        task_dir,
                        {
                            "status": "running",
                            "worker_pid": process.pid,
                            "last_alive_at": core.utc_now(),
                        },
                    )
    except OSError as error:
        terminal_status = "failed"
        failure_reason = str(error)
    duration_seconds = time.monotonic() - start
    if terminal_status == "completed" and exit_code != 0:
        terminal_status = "failed"
        failure_reason = f"worker exited with code {exit_code}"

    result = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_RESULT",
        "task_id": task_id,
        "worker": worker,
        "terminal_status": terminal_status,
        "exit_code": exit_code,
        "failure_reason": failure_reason,
        "duration_seconds": round(duration_seconds, 3),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "started_at": started_at,
        "finished_at": core.utc_now(),
    }
    result_path = task_dir / "result.json"
    core.atomic_json(result_path, result)

    evidence = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_EVIDENCE",
        "task_id": task_id,
        "worker": worker,
        "command": command if config["prompt_via"] != "arg" else (
            [*config["command"], "<prompt>"]
        ),
        "prompt_file": str(prompt),
        "prompt_sha256": core.sha256_file(prompt),
        "worker_config": {
            "prompt_via": config["prompt_via"],
            "timeout_seconds": config["timeout_seconds"],
            **config["extras"],
        },
        "started_at": started_at,
        "finished_at": result["finished_at"],
    }
    if wake_target is not None:
        evidence["wake_target"] = wake_target
    evidence_path = task_dir / "evidence.json"
    core.atomic_json(evidence_path, evidence)

    emitted = core.write_terminal_event(
        project,
        task_id=task_id,
        terminal_status=terminal_status,
        result_path=result_path,
        evidence_path=evidence_path,
        state_dir=state_dir,
        wake_target=wake_target,
    )

    descriptor = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": TASK_KIND,
        "task_id": task_id,
        "worker": worker,
        "prompt_file": str(prompt),
        "task_dir": str(task_dir),
        "created_at": started_at,
    }
    if descriptor_path.exists():
        with contextlib.suppress(OSError, core.OrchestratorError):
            descriptor.update(core.load_object(descriptor_path))
    descriptor.update(
        status=terminal_status,
        finished_at=result["finished_at"],
        event_path=emitted["event_path"],
        signal_path=emitted["signal_path"],
    )
    core.atomic_json(descriptor_path, descriptor)
    return {**descriptor, "descriptor_path": str(descriptor_path)}
