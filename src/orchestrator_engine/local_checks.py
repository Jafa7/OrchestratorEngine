"""First-class deterministic local checks with duration-aware execution."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import shlex
import signal
import statistics
import subprocess
import sys
import threading
import time
import tomllib
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import binding, core, platform_runtime, verification, worker_lease

CHECK_KIND = "ORCHESTRATOR_LOCAL_CHECK"
HISTORY_KIND = "ORCHESTRATOR_CHECK_DURATION_HISTORY"
SOURCE_KIND = "local_check"
CONFIG_NAME = "checks.toml"
HISTORY_NAME = "check-history.json"
EXECUTION_MODES = {"auto", "foreground", "detached"}
WAKE_POLICIES = {"auto", "always", "on-failure", "never"}
VERIFICATION_LEVELS = {"structural", "focused", "full"}
TERMINAL_STATUSES = {"passed", "failed", "errored", "cancelled"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DEFAULT_LONG_THRESHOLD_SECONDS = 30.0
DEFAULT_TAIL_LINES = 80
DEFAULT_FAILURE_EXCERPT_LINES = 20
MAX_HISTORY_SAMPLES = 10
MAX_COMMANDS = 32
MAX_ARGV_ITEMS = 256
MAX_ARG_LENGTH = 4096
MAX_LABEL_LENGTH = 128
MAX_SUITE_ARGV_BYTES = 64 * 1024
MAX_TAIL_BYTES = 64 * 1024
MAX_TAIL_LINE_CHARS = 4096
STARTING_GRACE_SECONDS = 30.0


class LocalCheckError(RuntimeError):
    """A deterministic local-check contract failure."""


@dataclass(frozen=True)
class CommandSpec:
    label: str
    argv: list[str]
    cwd: Path
    required: bool = True
    timeout_seconds: float | None = None


def validate_id(value: str, *, field: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise LocalCheckError(f"invalid {field}: {value!r}")
    return value


def config_path(project_root: Path, *, state_dir: str) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / CONFIG_NAME


def history_path(project_root: Path, *, state_dir: str) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / HISTORY_NAME


def check_dir(project_root: Path, check_id: str, *, state_dir: str) -> Path:
    return verification.checks_root(project_root, state_dir=state_dir) / validate_id(
        check_id, field="check id"
    )


def descriptor_path(project_root: Path, check_id: str, *, state_dir: str) -> Path:
    return check_dir(project_root, check_id, state_dir=state_dir) / "check.json"


@contextlib.contextmanager
def file_lock(path: Path):
    with platform_runtime.exclusive_file_lock(path):
        yield


def read_suite(
    project_root: Path,
    *,
    suite: str,
    state_dir: str,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    path = config_path(project, state_dir=state_dir)
    if not path.is_file():
        raise LocalCheckError(f"local check config not found: {path}")
    try:
        root = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise LocalCheckError(f"invalid local check config: {path}: {error}") from error
    suites = root.get("suites")
    raw = suites.get(suite) if isinstance(suites, dict) else None
    if not isinstance(raw, dict):
        raise LocalCheckError(f"suite {suite!r} not found in {path}")
    verification_level = raw.get("verification", "focused")
    if verification_level not in VERIFICATION_LEVELS:
        raise LocalCheckError(
            f"suite {suite!r} verification must be structural, focused or full"
        )
    expected = raw.get("expected_duration_seconds")
    if expected is not None and (
        isinstance(expected, bool)
        or not isinstance(expected, int | float)
        or expected < 0
    ):
        raise LocalCheckError("expected_duration_seconds must be non-negative")
    raw_commands = raw.get("commands")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise LocalCheckError(f"suite {suite!r} must define commands")
    if len(raw_commands) > MAX_COMMANDS:
        raise LocalCheckError(f"suite {suite!r} exceeds {MAX_COMMANDS} commands")
    commands: list[CommandSpec] = []
    artifact_labels: set[str] = set()
    argv_bytes = 0
    for index, item in enumerate(raw_commands, start=1):
        if not isinstance(item, dict):
            raise LocalCheckError(f"suite {suite!r} command {index} must be a table")
        argv = item.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > MAX_ARGV_ITEMS
            or not all(isinstance(arg, str) and arg for arg in argv)
            or any(len(arg) > MAX_ARG_LENGTH for arg in argv)
        ):
            raise LocalCheckError(
                f"suite {suite!r} command {index} has invalid argv"
            )
        argv_bytes += sum(len(arg.encode("utf-8")) for arg in argv)
        if argv_bytes > MAX_SUITE_ARGV_BYTES:
            raise LocalCheckError(
                f"suite {suite!r} argv exceeds {MAX_SUITE_ARGV_BYTES} bytes"
            )
        label = item.get("label", f"command-{index}")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > MAX_LABEL_LENGTH
        ):
            raise LocalCheckError(f"suite {suite!r} command {index} label is invalid")
        artifact_label = safe_label(label)
        if artifact_label in artifact_labels:
            raise LocalCheckError(
                f"suite {suite!r} command {index} label collides with another log"
            )
        artifact_labels.add(artifact_label)
        cwd_value = item.get("cwd", ".")
        if not isinstance(cwd_value, str):
            raise LocalCheckError(f"suite {suite!r} command {index} cwd is invalid")
        cwd = (project / cwd_value).resolve()
        try:
            cwd.relative_to(project)
        except ValueError as error:
            raise LocalCheckError(
                f"suite {suite!r} command {index} cwd escapes project root"
            ) from error
        timeout = item.get("timeout_seconds")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or timeout <= 0
        ):
            raise LocalCheckError(
                f"suite {suite!r} command {index} timeout must be positive"
            )
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise LocalCheckError(
                f"suite {suite!r} command {index} required must be boolean"
            )
        commands.append(
            CommandSpec(
                label=label.strip(),
                argv=list(argv),
                cwd=cwd,
                required=required,
                timeout_seconds=float(timeout) if timeout is not None else None,
            )
        )
    fingerprint_value = {
        "suite": suite,
        "verification": verification_level,
        "commands": [
            {
                "label": item.label,
                "argv": item.argv,
                "cwd": str(item.cwd.relative_to(project)),
                "required": item.required,
                "timeout_seconds": item.timeout_seconds,
            }
            for item in commands
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_value, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        "suite": suite,
        "verification": verification_level,
        "expected_duration_seconds": (
            float(expected) if expected is not None else None
        ),
        "commands": commands,
        "fingerprint": fingerprint,
        "config_path": str(path),
    }


def load_history(project_root: Path, *, state_dir: str) -> dict[str, Any]:
    path = history_path(project_root, state_dir=state_dir)
    if not path.is_file():
        return {
            "schema_version": core.SCHEMA_VERSION,
            "kind": HISTORY_KIND,
            "entries": {},
        }
    value = core.load_object(path)
    if value.get("kind") != HISTORY_KIND or not isinstance(value.get("entries"), dict):
        raise LocalCheckError(f"invalid check duration history: {path}")
    return value


def duration_samples(history: dict[str, Any], fingerprint: str) -> list[float]:
    rows = history.get("entries", {}).get(fingerprint, [])
    if not isinstance(rows, list):
        return []
    return [
        float(row["duration_seconds"])
        for row in rows
        if isinstance(row, dict)
        and row.get("status") == "passed"
        and isinstance(row.get("duration_seconds"), int | float)
    ]


def plan_check(
    project_root: Path,
    *,
    suite: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    long_threshold_seconds: float = DEFAULT_LONG_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    if long_threshold_seconds <= 0:
        raise LocalCheckError("long threshold must be positive")
    project = project_root.expanduser().resolve()
    spec = read_suite(project, suite=suite, state_dir=state_dir)
    history = load_history(project, state_dir=state_dir)
    samples = duration_samples(history, str(spec["fingerprint"]))
    expected = spec["expected_duration_seconds"]
    if expected is not None:
        estimate = float(expected)
        source = "configured"
    elif samples:
        estimate = float(statistics.median(samples))
        source = "successful_history_median"
    else:
        estimate = None
        source = "unknown"
    if estimate is not None and estimate > long_threshold_seconds:
        execution = "detached"
        reason = "estimated_duration_exceeds_threshold"
    elif estimate is not None:
        execution = "foreground"
        reason = "estimated_duration_within_threshold"
    elif spec["verification"] == "full":
        execution = "detached"
        reason = "unknown_full_verification_duration"
    else:
        execution = "foreground"
        reason = "unknown_non_full_duration"
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_LOCAL_CHECK_PLAN",
        "project_root": str(project),
        "suite": suite,
        "verification": spec["verification"],
        "fingerprint": spec["fingerprint"],
        "command_count": len(spec["commands"]),
        "estimated_duration_seconds": estimate,
        "estimate_source": source,
        "successful_history_samples": len(samples),
        "long_threshold_seconds": float(long_threshold_seconds),
        "recommended_execution": execution,
        "reason": reason,
        "planned_at": core.utc_now(),
    }


def safe_label(value: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in value.strip()
    ).strip("-")
    return cleaned or "command"


def relative_path(path: Path, project_root: Path) -> str:
    return str(path.resolve().relative_to(project_root.resolve()))


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if hasattr(os, "killpg"):
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        with contextlib.suppress(OSError):
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if hasattr(os, "killpg"):
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with contextlib.suppress(OSError):
                process.kill()
        process.wait(timeout=5)


def read_lines(stream, output: queue.Queue[str]) -> None:
    for line in stream:
        output.put(line)


def reap_detached_process(process: subprocess.Popen[bytes]) -> None:
    """Release the local child handle without blocking the dispatching thread."""

    process.wait()


def run_command(
    spec: CommandSpec,
    *,
    project_root: Path,
    artifacts_dir: Path,
    full_log,
) -> dict[str, Any]:
    log_path = artifacts_dir / f"{safe_label(spec.label)}.log"
    started_at = core.utc_now()
    started = time.monotonic()
    tail: deque[str] = deque(maxlen=DEFAULT_TAIL_LINES)
    output_hasher = hashlib.sha256()
    output_bytes = 0
    line_count = 0
    timed_out = False
    full_log.write(f"\n--- {spec.label}: {shlex.join(spec.argv)} ---\n")
    full_log.flush()
    try:
        process = subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as error:
        message = str(error)
        log_path.write_text(message + "\n", encoding="utf-8")
        full_log.write(message + "\n")
        return {
            "label": spec.label,
            "required": spec.required,
            "status": "errored",
            "exit_code": None,
            "started_at": started_at,
            "finished_at": core.utc_now(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "cwd": relative_path(spec.cwd, project_root),
            "argv": spec.argv,
            "command": shlex.join(spec.argv),
            "log_path": relative_path(log_path, project_root),
            "output_tail": [message],
            "output_line_count": 1,
            "error": message,
        }
    assert process.stdout is not None
    deadline = (
        time.monotonic() + spec.timeout_seconds
        if spec.timeout_seconds is not None
        else None
    )
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=read_lines, args=(process.stdout, lines))
    reader.start()
    with log_path.open("w", encoding="utf-8") as command_log:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                terminate_process(process)
            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            command_log.write(line)
            full_log.write(line)
            encoded = line.encode("utf-8", errors="replace")
            output_hasher.update(encoded)
            output_bytes += len(encoded)
            tail.append(line.rstrip("\n")[-MAX_TAIL_LINE_CHARS:])
            while sum(len(item.encode("utf-8")) for item in tail) > MAX_TAIL_BYTES:
                tail.popleft()
            line_count += 1
        reader.join(timeout=1)
        while not lines.empty():
            line = lines.get_nowait()
            command_log.write(line)
            full_log.write(line)
            encoded = line.encode("utf-8", errors="replace")
            output_hasher.update(encoded)
            output_bytes += len(encoded)
            tail.append(line.rstrip("\n")[-MAX_TAIL_LINE_CHARS:])
            while sum(len(item.encode("utf-8")) for item in tail) > MAX_TAIL_BYTES:
                tail.popleft()
            line_count += 1
    process.stdout.close()
    exit_code = process.poll()
    if exit_code is None:
        terminate_process(process)
        exit_code = process.poll()
    if timed_out:
        status = "timed_out"
    elif exit_code == 0:
        status = "passed"
    else:
        status = "failed"
    result: dict[str, Any] = {
        "label": spec.label,
        "required": spec.required,
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": core.utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "cwd": relative_path(spec.cwd, project_root),
        "argv": spec.argv,
        "command": shlex.join(spec.argv),
        "log_path": relative_path(log_path, project_root),
        "output_tail": list(tail) if status != "passed" else [],
        "output_line_count": line_count,
        "output_bytes": output_bytes,
        "output_sha256": output_hasher.hexdigest(),
    }
    if timed_out:
        result["error"] = f"command timed out after {spec.timeout_seconds}s"
    return result


def overall_status(commands: list[dict[str, Any]]) -> str:
    required = [item for item in commands if item.get("required", True)]
    if any(item.get("status") == "errored" for item in required):
        return "errored"
    if any(item.get("status") != "passed" for item in required):
        return "failed"
    return "passed"


def build_summary(result: dict[str, Any]) -> str:
    lines = [
        f"Status: {result['status']}",
        f"Check: {result['check_id']}",
        f"Suite: {result['suite']}",
        f"Duration: {result['duration_seconds']:.3f}s",
        "",
        "Commands:",
    ]
    for command in result["commands"]:
        lines.append(
            f"- {command['label']} [{command['status']}] "
            f"{command['duration_seconds']:.3f}s exit={command['exit_code']}"
        )
    failing = [
        item
        for item in result["commands"]
        if item.get("required", True) and item.get("status") != "passed"
    ]
    if failing:
        lines.extend(["", "Failure excerpts:"])
        for command in failing:
            lines.append(f"[{command['label']}] {command['status']}")
            lines.extend(
                f"  {line}"
                for line in command.get("output_tail", [])[
                    -DEFAULT_FAILURE_EXCERPT_LINES:
                ]
            )
    lines.extend(
        [
            "",
            "Artifacts:",
            f"- result: {result['result_path']}",
            f"- summary: {result['summary_path']}",
            f"- full log: {result['log_path']}",
        ]
    )
    return "\n".join(lines) + "\n"


def record_history(
    project_root: Path,
    *,
    state_dir: str,
    fingerprint: str,
    sample: dict[str, Any],
) -> None:
    path = history_path(project_root, state_dir=state_dir)
    with file_lock(path.with_suffix(".lock")):
        history = load_history(project_root, state_dir=state_dir)
        entries = history["entries"]
        rows = entries.get(fingerprint, [])
        if not isinstance(rows, list):
            rows = []
        rows.append(sample)
        entries[fingerprint] = rows[-MAX_HISTORY_SAMPLES:]
        history["updated_at"] = core.utc_now()
        core.atomic_json(path, history)


def resolved_wake_policy(requested: str, execution: str) -> str:
    if requested not in WAKE_POLICIES:
        raise LocalCheckError(f"unsupported wake policy: {requested}")
    if requested == "auto":
        return "always" if execution == "detached" else "never"
    return requested


def capture_wake_target(
    project_root: Path,
    *,
    state_dir: str,
    wake_policy: str,
) -> dict[str, Any] | None:
    if wake_policy == "never":
        return None
    bound = binding.require_binding(project_root, state_dir=state_dir)
    return binding.wake_target_from_binding(bound)


def supervisor_command(
    project_root: Path,
    *,
    check_id: str,
    state_dir: str,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "orchestrator_engine.cli",
        "--project-root",
        str(project_root),
        "--state-dir",
        state_dir,
        "check",
        "supervise",
        "--check-id",
        check_id,
    ]


def start_check(
    project_root: Path,
    *,
    check_id: str,
    suite: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    execution: str = "auto",
    wake_policy: str = "auto",
    long_threshold_seconds: float = DEFAULT_LONG_THRESHOLD_SECONDS,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    if execution not in EXECUTION_MODES:
        raise LocalCheckError(f"unsupported execution mode: {execution}")
    project = project_root.expanduser().resolve()
    check_id = validate_id(check_id, field="check id")
    spec = read_suite(project, suite=suite, state_dir=state_dir)
    plan = plan_check(
        project,
        suite=suite,
        state_dir=state_dir,
        long_threshold_seconds=long_threshold_seconds,
    )
    selected_execution = (
        str(plan["recommended_execution"]) if execution == "auto" else execution
    )
    if selected_execution == "detached":
        platform_runtime.require_detached_lifecycle("detached check run")
    selected_wake_policy = resolved_wake_policy(wake_policy, selected_execution)
    wake_target = capture_wake_target(
        project,
        state_dir=state_dir,
        wake_policy=selected_wake_policy,
    )
    directory = check_dir(project, check_id, state_dir=state_dir)
    path = directory / "check.json"
    try:
        verification.claim_check_owner(
            project,
            operation_id=check_id,
            operation_type="local_check",
            state_dir=state_dir,
        )
    except verification.VerificationError as error:
        raise LocalCheckError(str(error)) from error
    descriptor: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": CHECK_KIND,
        "check_id": check_id,
        "suite": suite,
        "verification": spec["verification"],
        "fingerprint": spec["fingerprint"],
        "status": "starting",
        "execution": selected_execution,
        "requested_execution": execution,
        "wake_policy": selected_wake_policy,
        "requested_wake_policy": wake_policy,
        "long_threshold_seconds": float(long_threshold_seconds),
        "plan": plan,
        "created_at": core.utc_now(),
        "check_dir": str(directory),
    }
    if wake_target is not None:
        descriptor["wake_target"] = wake_target
    directory.mkdir(parents=True, exist_ok=True)
    if not core.claim_json(path, descriptor):
        existing = core.load_object(path)
        if (
            existing.get("suite") == suite
            and existing.get("fingerprint") == spec["fingerprint"]
            and existing.get("execution") == selected_execution
            and existing.get("wake_policy") == selected_wake_policy
        ):
            return {**existing, "descriptor_path": str(path), "idempotent": True}
        raise LocalCheckError(
            f"check already exists with different options: {check_id}"
        )
    if selected_execution == "foreground":
        result = supervise_check(project, check_id=check_id, state_dir=state_dir)
        return {**result, "descriptor_path": str(path), "idempotent": False}
    supervisor_log = directory / "supervisor.log"
    try:
        with supervisor_log.open("ab") as log:
            process = popen_factory(
                supervisor_command(
                    project, check_id=check_id, state_dir=state_dir
                ),
                cwd=str(project),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as error:
        finalize_launch_failure(
            project,
            descriptor,
            state_dir=state_dir,
            error=str(error),
        )
        raise LocalCheckError(f"could not launch check supervisor: {error}") from error
    launch_error: str | None = None
    with file_lock(path.with_suffix(".lock")):
        current = core.load_object(path)
        if current.get("status") == "starting":
            identity = worker_lease.process_identity(process.pid)
            if identity is None:
                launch_error = "check supervisor exited before identity was recorded"
            else:
                current.update(
                    supervisor_pid=int(process.pid),
                    supervisor_identity=identity,
                )
                core.atomic_json(path, current)
        descriptor = current
    if launch_error is not None:
        terminate_process(process)
        final = finalize_launch_failure(
            project,
            descriptor,
            state_dir=state_dir,
            error=launch_error,
        )
        raise LocalCheckError(
            f"could not launch check supervisor: {final.get('error')}"
        )
    threading.Thread(
        target=reap_detached_process,
        args=(process,),
        daemon=True,
    ).start()
    return {
        **descriptor,
        "descriptor_path": str(path),
        "idempotent": False,
    }


def supervise_check(
    project_root: Path,
    *,
    check_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    try:
        verification.claim_check_owner(
            project,
            operation_id=check_id,
            operation_type="local_check",
            state_dir=state_dir,
        )
    except verification.VerificationError as error:
        raise LocalCheckError(str(error)) from error
    path = descriptor_path(project, check_id, state_dir=state_dir)
    lock = path.with_suffix(".lock")
    with file_lock(lock):
        descriptor = core.load_object(path)
        if descriptor.get("status") in TERMINAL_STATUSES:
            return descriptor
        if descriptor.get("status") == "running":
            raise LocalCheckError("check is already owned by a supervisor")
        identity = worker_lease.process_identity(os.getpid())
        descriptor.update(
            status="running",
            supervisor_pid=os.getpid(),
            supervisor_identity=identity,
            started_at=core.utc_now(),
        )
        core.atomic_json(path, descriptor)
    spec = read_suite(project, suite=str(descriptor["suite"]), state_dir=state_dir)
    if spec["fingerprint"] != descriptor.get("fingerprint"):
        return finalize_launch_failure(
            project,
            descriptor,
            state_dir=state_dir,
            error="check suite changed after dispatch",
        )
    started = time.monotonic()
    full_log_path = path.parent / "full.log"
    with full_log_path.open("w", encoding="utf-8") as full_log:
        command_results = [
            run_command(
                command,
                project_root=project,
                artifacts_dir=path.parent,
                full_log=full_log,
            )
            for command in spec["commands"]
        ]
    status = overall_status(command_results)
    duration = round(time.monotonic() - started, 3)
    result_path = path.parent / "verification-result.json"
    summary_path = path.parent / "summary.txt"
    evidence_path = path.parent / "evidence.json"
    result: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": verification.VERIFICATION_RESULT_KIND,
        "check_id": check_id,
        "suite": descriptor["suite"],
        "status": status,
        "exit_code": 0 if status == "passed" else 1,
        "started_at": descriptor["started_at"],
        "finished_at": core.utc_now(),
        "duration_seconds": duration,
        "commands": command_results,
        "result_path": relative_path(result_path, project),
        "summary_path": relative_path(summary_path, project),
        "log_path": relative_path(full_log_path, project),
        "execution": descriptor["execution"],
        "fingerprint": descriptor["fingerprint"],
    }
    summary_path.write_text(build_summary(result), encoding="utf-8")
    core.atomic_json(result_path, result)
    evidence: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_LOCAL_CHECK_EVIDENCE",
        "check_id": check_id,
        "suite": descriptor["suite"],
        "fingerprint": descriptor["fingerprint"],
        "execution": descriptor["execution"],
        "wake_policy": descriptor["wake_policy"],
        "plan": descriptor["plan"],
        "result_path": str(result_path),
        "result_sha256": core.sha256_file(result_path),
        "summary_path": str(summary_path),
        "summary_sha256": core.sha256_file(summary_path),
        "full_log_path": str(full_log_path),
        "full_log_sha256": core.sha256_file(full_log_path),
        "recorded_at": core.utc_now(),
    }
    if isinstance(descriptor.get("wake_target"), dict):
        evidence["wake_target"] = descriptor["wake_target"]
    core.atomic_json(evidence_path, evidence)
    record_history(
        project,
        state_dir=state_dir,
        fingerprint=str(descriptor["fingerprint"]),
        sample={
            "check_id": check_id,
            "status": status,
            "duration_seconds": duration,
            "finished_at": result["finished_at"],
        },
    )
    terminal_status = "completed" if status == "passed" else "failed"
    emit_signal = descriptor["wake_policy"] == "always" or (
        descriptor["wake_policy"] == "on-failure" and status != "passed"
    )
    event = core.write_followup_event(
        project,
        operation_id=check_id,
        source_kind=SOURCE_KIND,
        terminal_status=terminal_status,
        result_path=result_path,
        evidence_path=evidence_path,
        state_dir=state_dir,
        wake_target=(
            descriptor.get("wake_target")
            if isinstance(descriptor.get("wake_target"), dict)
            else None
        ),
        emit_signal=emit_signal,
    )
    descriptor.update(
        status=status,
        finished_at=result["finished_at"],
        duration_seconds=duration,
        result_path=str(result_path),
        summary_path=str(summary_path),
        evidence_path=str(evidence_path),
        event_id=event["event"]["event_id"],
        event_path=event["event_path"],
        signal_path=event["signal_path"],
    )
    core.atomic_json(path, descriptor)
    return descriptor


def finalize_launch_failure(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
    error: str,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    directory = Path(descriptor["check_dir"])
    result_path = directory / "verification-result.json"
    summary_path = directory / "summary.txt"
    evidence_path = directory / "evidence.json"
    result = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": verification.VERIFICATION_RESULT_KIND,
        "check_id": descriptor["check_id"],
        "suite": descriptor["suite"],
        "status": "errored",
        "exit_code": None,
        "started_at": descriptor.get("started_at") or descriptor["created_at"],
        "finished_at": core.utc_now(),
        "duration_seconds": 0.0,
        "commands": [],
        "error": error[:1000],
        "result_path": relative_path(result_path, project),
        "summary_path": relative_path(summary_path, project),
        "log_path": relative_path(directory / "supervisor.log", project),
    }
    summary_path.write_text(
        f"Status: errored\nCheck: {descriptor['check_id']}\nError: {error[:1000]}\n",
        encoding="utf-8",
    )
    core.atomic_json(result_path, result)
    evidence = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_LOCAL_CHECK_EVIDENCE",
        "check_id": descriptor["check_id"],
        "suite": descriptor["suite"],
        "fingerprint": descriptor["fingerprint"],
        "execution": descriptor["execution"],
        "wake_policy": descriptor["wake_policy"],
        "plan": descriptor["plan"],
        "result_path": str(result_path),
        "result_sha256": core.sha256_file(result_path),
        "summary_path": str(summary_path),
        "summary_sha256": core.sha256_file(summary_path),
        "recorded_at": core.utc_now(),
        "error": error[:1000],
    }
    if isinstance(descriptor.get("wake_target"), dict):
        evidence["wake_target"] = descriptor["wake_target"]
    core.atomic_json(evidence_path, evidence)
    event = core.write_followup_event(
        project,
        operation_id=str(descriptor["check_id"]),
        source_kind=SOURCE_KIND,
        terminal_status="failed",
        result_path=result_path,
        evidence_path=evidence_path,
        state_dir=state_dir,
        wake_target=(
            descriptor.get("wake_target")
            if isinstance(descriptor.get("wake_target"), dict)
            else None
        ),
        emit_signal=descriptor.get("wake_policy") in {"always", "on-failure"},
    )
    descriptor.update(
        status="errored",
        finished_at=result["finished_at"],
        error=error[:1000],
        result_path=str(result_path),
        summary_path=str(summary_path),
        evidence_path=str(evidence_path),
        event_id=event["event"]["event_id"],
        event_path=event["event_path"],
        signal_path=event["signal_path"],
    )
    core.atomic_json(
        descriptor_path(
            project, str(descriptor["check_id"]), state_dir=state_dir
        ),
        descriptor,
    )
    return descriptor


def timestamp_age(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return max((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds(), 0.0)


def recover_completed_check(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
) -> dict[str, Any] | None:
    """Recover a descriptor when durable terminal artifacts already exist."""

    project = project_root.expanduser().resolve()
    directory = Path(str(descriptor["check_dir"]))
    result_path = directory / "verification-result.json"
    evidence_path = directory / "evidence.json"
    summary_path = directory / "summary.txt"
    if not (
        result_path.is_file()
        and evidence_path.is_file()
        and summary_path.is_file()
    ):
        return None
    try:
        result = core.load_object(result_path)
        evidence = core.load_object(evidence_path)
    except (OSError, core.OrchestratorError):
        return None
    status = result.get("status")
    if (
        result.get("kind") != verification.VERIFICATION_RESULT_KIND
        or result.get("check_id") != descriptor.get("check_id")
        or status not in TERMINAL_STATUSES
        or evidence.get("kind") != "ORCHESTRATOR_LOCAL_CHECK_EVIDENCE"
        or evidence.get("check_id") != descriptor.get("check_id")
    ):
        return None
    emit_signal = descriptor.get("wake_policy") == "always" or (
        descriptor.get("wake_policy") == "on-failure" and status != "passed"
    )
    event = core.write_followup_event(
        project,
        operation_id=str(descriptor["check_id"]),
        source_kind=SOURCE_KIND,
        terminal_status="completed" if status == "passed" else "failed",
        result_path=result_path,
        evidence_path=evidence_path,
        state_dir=state_dir,
        wake_target=(
            descriptor.get("wake_target")
            if isinstance(descriptor.get("wake_target"), dict)
            else None
        ),
        emit_signal=emit_signal,
    )
    descriptor.update(
        status=status,
        finished_at=result.get("finished_at") or core.utc_now(),
        duration_seconds=result.get("duration_seconds"),
        result_path=str(result_path),
        summary_path=str(summary_path),
        evidence_path=str(evidence_path),
        event_id=event["event"]["event_id"],
        event_path=event["event_path"],
        signal_path=event["signal_path"],
        recovered_at=core.utc_now(),
    )
    core.atomic_json(
        descriptor_path(
            project, str(descriptor["check_id"]), state_dir=state_dir
        ),
        descriptor,
    )
    return descriptor


def reap_checks(
    project_root: Path,
    *,
    check_id: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Finalize checks whose detached supervisor is proven gone."""

    platform_runtime.require_detached_lifecycle("check reap")
    project = project_root.expanduser().resolve()
    root = verification.checks_root(project, state_dir=state_dir)
    paths = (
        [descriptor_path(project, check_id, state_dir=state_dir)]
        if check_id is not None
        else sorted(root.glob("*/check.json"))
    )
    if check_id is not None and not paths[0].is_file():
        raise LocalCheckError(f"unknown check: {check_id}")
    outcomes: list[dict[str, Any]] = []
    for path in paths:
        with file_lock(path.with_suffix(".lock")):
            try:
                descriptor = core.load_object(path)
            except (OSError, core.OrchestratorError) as error:
                outcomes.append(
                    {
                        "check_id": path.parent.name,
                        "status": "invalid",
                        "reason": str(error)[:1000],
                    }
                )
                continue
            if descriptor.get("kind") != CHECK_KIND:
                outcomes.append(
                    {
                        "check_id": descriptor.get("check_id") or path.parent.name,
                        "status": "invalid",
                        "reason": "descriptor contract is invalid",
                    }
                )
                continue
            stored_status = descriptor.get("status")
            if stored_status in TERMINAL_STATUSES:
                continue
            process = worker_lease.identity_state(descriptor.get("supervisor_identity"))
            if process["state"] == "alive":
                outcomes.append(
                    {
                        "check_id": descriptor.get("check_id"),
                        "status": "supervisor_alive",
                    }
                )
                continue
            if process["state"] == "unknown":
                outcomes.append(
                    {
                        "check_id": descriptor.get("check_id"),
                        "status": "unsafe_missing_identity",
                    }
                )
                continue
            recovered = recover_completed_check(
                project,
                descriptor,
                state_dir=state_dir,
            )
            if recovered is not None:
                outcomes.append(
                    {
                        "check_id": descriptor.get("check_id"),
                        "status": "recovered",
                        "terminal_status": recovered.get("status"),
                        "event_path": recovered.get("event_path"),
                    }
                )
                continue
            failure_kind = (
                "supervisor_never_claimed"
                if stored_status == "starting"
                else "supervisor_not_alive"
            )
            final = finalize_launch_failure(
                project,
                descriptor,
                state_dir=state_dir,
                error=(
                    "detached local check supervisor exited before finalization: "
                    f"{failure_kind}"
                ),
            )
            outcomes.append(
                {
                    "check_id": descriptor.get("check_id"),
                    "status": "reaped",
                    "failure_kind": failure_kind,
                    "event_path": final.get("event_path"),
                }
            )
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_LOCAL_CHECK_REAP_REPORT",
        "project_root": str(project),
        "reaped_count": sum(item.get("status") == "reaped" for item in outcomes),
        "recovered_count": sum(
            item.get("status") == "recovered" for item in outcomes
        ),
        "outcomes": outcomes,
    }


def check_status(
    project_root: Path,
    *,
    check_id: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    root = verification.checks_root(project, state_dir=state_dir)
    paths = (
        [descriptor_path(project, check_id, state_dir=state_dir)]
        if check_id is not None
        else sorted(root.glob("*/check.json"))
    )
    if check_id is not None and not paths[0].is_file():
        raise LocalCheckError(f"unknown check: {check_id}")
    checks: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for path in paths:
        try:
            descriptor = core.load_object(path)
            if descriptor.get("kind") != CHECK_KIND:
                raise LocalCheckError("unexpected descriptor kind")
        except (OSError, RuntimeError, ValueError) as error:
            invalid.append({"path": str(path), "error": str(error)})
            continue
        effective_status = descriptor.get("status")
        process = None
        if effective_status == "running":
            process = worker_lease.identity_state(
                descriptor.get("supervisor_identity")
            )
            if process["state"] == "gone":
                effective_status = "crashed"
        elif effective_status == "starting":
            process = worker_lease.identity_state(
                descriptor.get("supervisor_identity")
            )
            age = timestamp_age(descriptor.get("created_at"))
            if process["state"] == "gone":
                effective_status = "crashed"
            elif age is not None and age > STARTING_GRACE_SECONDS:
                effective_status = "stalled"
        summary = {
            "check_id": descriptor.get("check_id"),
            "suite": descriptor.get("suite"),
            "status": effective_status,
            "stored_status": descriptor.get("status"),
            "execution": descriptor.get("execution"),
            "duration_seconds": descriptor.get("duration_seconds"),
            "result_path": descriptor.get("result_path"),
            "summary_path": descriptor.get("summary_path"),
            "evidence_path": descriptor.get("evidence_path"),
            "supervisor_process": process,
        }
        if effective_status in {"crashed", "stalled"}:
            summary["failure_kind"] = (
                "supervisor_not_alive"
                if effective_status == "crashed"
                else "supervisor_identity_unavailable"
            )
            summary["suggested_action"] = (
                "Run check reap; it will mutate state only when the recorded "
                "supervisor is proven gone."
            )
        checks.append(summary)
    counts: dict[str, int] = {}
    for item in checks:
        key = str(item["status"])
        counts[key] = counts.get(key, 0) + 1
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_LOCAL_CHECK_STATUS",
        "project_root": str(project),
        "check_count": len(checks),
        "status_counts": counts,
        "checks": checks,
        "invalid_count": len(invalid),
        "invalid": invalid,
        "checked_at": core.utc_now(),
    }
