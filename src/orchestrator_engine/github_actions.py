"""Detached GitHub Actions monitoring through an adopter-authenticated gh CLI."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import binding, core, platform_runtime, worker_lease

CONFIG_NAME = "integrations.toml"
SOURCE_KIND = "github_actions"
MONITOR_KIND = "GITHUB_ACTIONS_MONITOR"
EVIDENCE_KIND = "GITHUB_ACTIONS_MONITOR_EVIDENCE"
STATUS_KIND = "GITHUB_ACTIONS_MONITOR_STATUS"
TERMINAL_MONITOR_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "unavailable",
    "ambiguous",
}
WAKE_POLICIES = {"always", "on-failure", "action-required"}
TERMINAL_GITHUB_STATUS = "completed"
VIEW_FIELDS = (
    "attempt,conclusion,createdAt,databaseId,event,headBranch,headSha,"
    "startedAt,status,updatedAt,url,workflowDatabaseId,workflowName"
)
LIST_FIELDS = (
    "conclusion,createdAt,databaseId,headSha,status,updatedAt,url,workflowName"
)
JOBS_FIELDS = "jobs"
MAX_CAPTURE_BYTES = 64 * 1024
MAX_VIEW_BYTES = 256 * 1024
MAX_REASON_LENGTH = 1000
MAX_COMMAND_LENGTH = 4096
MAX_PROBLEM_JOBS = 20
MAX_PROBLEM_STEPS = 50
MAX_DIAGNOSTIC_NAME_LENGTH = 256
VIEW_TIMEOUT_SECONDS = 60.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
STARTING_GRACE_SECONDS = 30.0
CONTROL_POLL_SECONDS = 1.0
DISCOVERY_POLL_SECONDS = 5.0
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 900.0
TERMINATION_GRACE_SECONDS = 5.0
MAX_WORKFLOW_NAME_LENGTH = 256
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9_.-](?:[A-Za-z0-9_.-]{0,99})$"
)
HOSTNAME_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
MONITOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*)(?:bearer|token)\s+\S+"),
)
AUTH_MARKERS = (
    "authentication required",
    "not logged into",
    "gh auth login",
    "http 401",
    "bad credentials",
    "fine grained pat",
)
NOT_FOUND_MARKERS = (
    "could not find workflow run",
    "run not found",
    "http 404",
)
NETWORK_MARKERS = (
    "could not resolve host",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "tls handshake timeout",
    "context deadline exceeded",
    "temporary failure",
)
PROBLEM_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "startup_failure",
    "timed_out",
}
DIAGNOSTIC_CONCLUSIONS = PROBLEM_CONCLUSIONS - {"cancelled"}


class GitHubActionsError(RuntimeError):
    """A deterministic GitHub Actions monitor failure."""


def normalize_repository(value: str) -> str:
    repository = value.strip()
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise GitHubActionsError("repo must be OWNER/REPOSITORY")
    owner, name = repository.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."} or name.endswith(".git"):
        raise GitHubActionsError("repo must be a canonical OWNER/REPOSITORY")
    return f"{owner}/{name}"


def normalize_hostname(value: str) -> str:
    hostname = value.strip().lower()
    if not HOSTNAME_PATTERN.fullmatch(hostname) or ".." in hostname:
        raise GitHubActionsError("hostname is invalid")
    return hostname


def positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise GitHubActionsError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise GitHubActionsError(f"{field} must be a positive integer") from error
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise GitHubActionsError(f"{field} must be a positive decimal integer")
    return parsed


def validate_monitor_id(value: str) -> str:
    if not MONITOR_ID_PATTERN.fullmatch(value):
        raise GitHubActionsError(f"invalid monitor id: {value!r}")
    return value


def integrations_path(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / CONFIG_NAME


def load_config(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    path = integrations_path(project_root, state_dir=state_dir)
    if not path.is_file():
        raise GitHubActionsError(
            f"GitHub Actions integration is not configured: {path}"
        )
    try:
        root = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise GitHubActionsError(
            f"invalid integrations config: {path}: {error}"
        ) from error
    integrations = root.get("integrations")
    github = (
        integrations.get("github_actions")
        if isinstance(integrations, dict)
        else None
    )
    if not isinstance(github, dict):
        raise GitHubActionsError("missing [integrations.github_actions] config")
    if github.get("enabled") is not True:
        raise GitHubActionsError("GitHub Actions integration is not enabled")
    repositories = github.get("allowed_repositories")
    if not isinstance(repositories, list) or not repositories:
        raise GitHubActionsError("allowed_repositories must be a non-empty list")
    allowed: list[str] = []
    for item in repositories:
        if not isinstance(item, str):
            raise GitHubActionsError("allowed_repositories entries must be strings")
        allowed.append(normalize_repository(item))
    hosts = github.get("allowed_hosts", ["github.com"])
    if not isinstance(hosts, list) or not hosts:
        raise GitHubActionsError("allowed_hosts must be a non-empty list")
    allowed_hosts: list[str] = []
    for item in hosts:
        if not isinstance(item, str):
            raise GitHubActionsError("allowed_hosts entries must be strings")
        allowed_hosts.append(normalize_hostname(item))
    gh_command = github.get("gh_command", "gh")
    if not isinstance(gh_command, str) or not gh_command.strip():
        raise GitHubActionsError("gh_command must be one executable name or path")
    return {
        "path": str(path),
        "gh_command": gh_command.strip(),
        "allowed_repositories": allowed,
        "allowed_hosts": allowed_hosts,
    }


def monitor_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return (
        core.state_root(project_root, state_dir=state_dir)
        / "monitors"
        / "github-actions"
    )


def monitor_dir_for(
    project_root: Path,
    monitor_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return monitor_root(project_root, state_dir=state_dir) / validate_monitor_id(
        monitor_id
    )


@contextlib.contextmanager
def monitor_admission_lock(project_root: Path, *, state_dir: str):
    path = monitor_root(project_root, state_dir=state_dir) / ".admission.lock"
    with platform_runtime.exclusive_file_lock(path):
        yield


def default_monitor_id(
    *,
    hostname: str,
    repository: str,
    run_id: int,
    attempt: int | None,
) -> str:
    identity = f"{hostname}/{repository.casefold()}/{run_id}/{attempt or 'current'}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    attempt_part = f"-a{attempt}" if attempt is not None else ""
    return f"gha-{run_id}{attempt_part}-{suffix}"


def default_discovery_monitor_id(
    *,
    hostname: str,
    repository: str,
    expected_head_sha: str,
    workflow_name: str | None,
) -> str:
    identity = (
        f"{hostname}/{repository.casefold()}/{expected_head_sha}/"
        f"{workflow_name or '*'}"
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"gha-sha-{expected_head_sha[:12]}-{suffix}"


def repo_argument(hostname: str, repository: str) -> str:
    return f"{hostname}/{repository}"


def same_repository(left: object, right: object) -> bool:
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and left.casefold() == right.casefold()
    )


def repository_url_matches(
    value: object,
    *,
    hostname: str,
    repository: str,
) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or (parsed.hostname or "").casefold() != hostname.casefold()
    ):
        return False
    parts = [part for part in parsed.path.split("/") if part]
    try:
        owner, name = repository.split("/", 1)
    except ValueError:
        return False
    return (
        len(parts) >= 2
        and parts[0].casefold() == owner.casefold()
        and parts[1].casefold() == name.casefold()
    )


def capture_wake_target(
    project_root: Path,
    *,
    state_dir: str,
) -> dict[str, Any] | None:
    bound = binding.load_binding(project_root, state_dir=state_dir)
    return binding.wake_target_from_binding(bound) if bound is not None else None


def supervisor_command(
    project_root: Path,
    *,
    monitor_id: str,
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
        "ci",
        "supervise",
        "--monitor-id",
        monitor_id,
    ]


def supervisor_launch_path(directory: Path) -> Path:
    return directory / "supervisor-launch.json"


def bounded_reason(value: str, *, field: str) -> str:
    reason = value.strip()
    if not reason:
        raise GitHubActionsError(f"{field} is required")
    if len(reason) > MAX_REASON_LENGTH:
        raise GitHubActionsError(
            f"{field} must be at most {MAX_REASON_LENGTH} characters"
        )
    return reason


def start_monitor(
    project_root: Path,
    *,
    repository: str,
    run_id: int | str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
    hostname: str = "github.com",
    attempt: int | str | None = None,
    expected_head_sha: str | None = None,
    workflow_name: str | None = None,
    gh_command: str | None = None,
    timeout_seconds: float | None = None,
    wake_policy: str = "always",
    monitor_id: str | None = None,
    retry_of: str | None = None,
    retry_reason: str | None = None,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    platform_runtime.require_detached_lifecycle("ci watch")
    project = project_root.expanduser().resolve()
    config = load_config(project, state_dir=state_dir)
    normalized_repo = normalize_repository(repository)
    normalized_host = normalize_hostname(hostname)
    parsed_run_id = (
        positive_integer(run_id, field="run-id") if run_id is not None else None
    )
    parsed_attempt = (
        positive_integer(attempt, field="attempt") if attempt is not None else None
    )
    if normalized_repo.casefold() not in {
        item.casefold() for item in config["allowed_repositories"]
    }:
        raise GitHubActionsError(
            f"repository is not allowlisted: {normalized_repo}"
        )
    if normalized_host not in config["allowed_hosts"]:
        raise GitHubActionsError(f"hostname is not allowlisted: {normalized_host}")
    if wake_policy not in WAKE_POLICIES:
        raise GitHubActionsError(
            "wake policy must be one of: " + ", ".join(sorted(WAKE_POLICIES))
        )
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool):
            raise GitHubActionsError("timeout-seconds must be a finite positive number")
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as error:
            raise GitHubActionsError(
                "timeout-seconds must be a finite positive number"
            ) from error
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise GitHubActionsError("timeout-seconds must be a finite positive number")
    if expected_head_sha is not None:
        expected_head_sha = expected_head_sha.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", expected_head_sha):
            raise GitHubActionsError(
                "expected-head-sha must be a hexadecimal commit id"
            )
    if parsed_run_id is None:
        if attempt is not None:
            raise GitHubActionsError("attempt requires an explicit run-id")
        if expected_head_sha is None or len(expected_head_sha) not in {40, 64}:
            raise GitHubActionsError(
                "run discovery requires a full 40- or 64-character "
                "expected-head-sha"
            )
        if timeout_seconds is None:
            timeout_seconds = DEFAULT_DISCOVERY_TIMEOUT_SECONDS
    if workflow_name is not None:
        workflow_name = workflow_name.strip()
        if not workflow_name or len(workflow_name) > MAX_WORKFLOW_NAME_LENGTH:
            raise GitHubActionsError(
                "workflow-name must contain 1 to "
                f"{MAX_WORKFLOW_NAME_LENGTH} characters"
            )
        if any(character in workflow_name for character in ("\n", "\r", "\x00")):
            raise GitHubActionsError("workflow-name contains invalid characters")
        if parsed_run_id is not None:
            raise GitHubActionsError(
                "workflow-name is only valid when discovering a run"
            )
    executable = (gh_command or config["gh_command"]).strip()
    if not executable or any(
        character in executable for character in ("\n", "\r", "\x00")
    ):
        raise GitHubActionsError("gh-command must be one executable name or path")
    if len(executable) > MAX_COMMAND_LENGTH:
        raise GitHubActionsError(
            f"gh-command must be at most {MAX_COMMAND_LENGTH} characters"
        )
    normalized_retry_reason = (
        bounded_reason(retry_reason or "", field="retry reason")
        if retry_of is not None
        else None
    )
    run_identity = {
        "hostname": normalized_host,
        "repository": normalized_repo,
        "run_id": parsed_run_id,
        "requested_run_id": parsed_run_id,
        "attempt": parsed_attempt,
        "workflow_name": workflow_name,
    }
    dispatch_identity = {
        "hostname": normalized_host,
        "repository": normalized_repo,
        "requested_run_id": parsed_run_id,
        "attempt": parsed_attempt,
        "workflow_name": workflow_name,
        "expected_head_sha": expected_head_sha,
        "wake_policy": wake_policy,
        "timeout_seconds": timeout_seconds,
        "gh_command": executable,
        "retry_of": retry_of,
        "retry_reason": normalized_retry_reason,
    }
    resolved_monitor_id = validate_monitor_id(
        monitor_id
        or (
            default_monitor_id(
                hostname=normalized_host,
                repository=normalized_repo,
                run_id=parsed_run_id,
                attempt=parsed_attempt,
            )
            if parsed_run_id is not None
            else default_discovery_monitor_id(
                hostname=normalized_host,
                repository=normalized_repo,
                expected_head_sha=str(expected_head_sha),
                workflow_name=workflow_name,
            )
        )
    )
    directory = monitor_dir_for(project, resolved_monitor_id, state_dir=state_dir)
    descriptor_path = directory / "monitor.json"
    descriptor = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": MONITOR_KIND,
        "monitor_id": resolved_monitor_id,
        "source_kind": SOURCE_KIND,
        "status": "starting",
        **run_identity,
        "expected_head_sha": expected_head_sha,
        "gh_command": executable,
        "wake_policy": wake_policy,
        "timeout_seconds": timeout_seconds,
        "monitor_dir": str(directory),
        "supervisor_log": str(directory / "supervisor.log"),
        "created_at": core.utc_now(),
    }
    if parsed_run_id is None:
        descriptor["discovery_query_count"] = 0
    if retry_of is not None:
        descriptor["retry_of"] = validate_monitor_id(retry_of)
        descriptor["retry_reason"] = normalized_retry_reason
    wake_target = capture_wake_target(project, state_dir=state_dir)
    if wake_target is not None:
        descriptor["wake_target"] = wake_target
    with monitor_admission_lock(project, state_dir=state_dir):
        for existing_path in sorted(
            monitor_root(project, state_dir=state_dir).glob("*/monitor.json")
        ):
            existing = core.load_object(existing_path)
            existing_dispatch = {
                key: (
                    existing.get("run_id")
                    if key == "requested_run_id"
                    and "requested_run_id" not in existing
                    else existing.get(key)
                )
                for key in (
                    "hostname",
                    "repository",
                    "requested_run_id",
                    "attempt",
                    "workflow_name",
                    "expected_head_sha",
                    "wake_policy",
                    "timeout_seconds",
                    "gh_command",
                    "retry_of",
                    "retry_reason",
                )
            }
            if existing_path == descriptor_path:
                comparable_existing = {
                    **existing_dispatch,
                    "repository": str(existing_dispatch["repository"]).casefold(),
                }
                comparable_dispatch = {
                    **dispatch_identity,
                    "repository": str(dispatch_identity["repository"]).casefold(),
                }
                if comparable_existing != comparable_dispatch:
                    raise GitHubActionsError(
                        "monitor already exists with different dispatch options: "
                        f"{resolved_monitor_id}"
                    )
                return {
                    **existing,
                    "descriptor_path": str(existing_path),
                    "idempotent": True,
                }
            if existing.get("status") in TERMINAL_MONITOR_STATUSES:
                continue
            same_host_repo = (
                existing.get("hostname") == run_identity["hostname"]
                and same_repository(
                    existing.get("repository"), run_identity["repository"]
                )
            )
            same_exact_run = (
                parsed_run_id is not None
                and existing.get("run_id") == parsed_run_id
            )
            existing_requested = existing.get(
                "requested_run_id", existing.get("run_id")
            )
            same_discovery = (
                parsed_run_id is None
                and existing_requested is None
                and existing.get("expected_head_sha") == expected_head_sha
                and existing.get("workflow_name") == workflow_name
            )
            if same_host_repo and (same_exact_run or same_discovery):
                raise GitHubActionsError(
                    "an active monitor already owns this run or discovery: "
                    f"{existing.get('monitor_id')}"
                )
        directory.mkdir(parents=True, exist_ok=True)
        if not core.claim_json(descriptor_path, descriptor):
            raise GitHubActionsError(f"monitor already exists: {resolved_monitor_id}")

    supervisor_log = Path(descriptor["supervisor_log"])
    try:
        with supervisor_log.open("ab") as log:
            process = popen_factory(
                supervisor_command(
                    project,
                    monitor_id=resolved_monitor_id,
                    state_dir=state_dir,
                ),
                cwd=str(project),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as error:
        message = redact_text(str(error))[:MAX_REASON_LENGTH]
        finalize_monitor(
            project,
            descriptor,
            {
                "monitor_status": "failed",
                "failure_kind": "supervisor_launch_failed",
                "error": message,
                "initial_view": None,
                "watch": None,
                "final_view": None,
            },
            state_dir=state_dir,
            started_at=str(descriptor["created_at"]),
            duration_seconds=0.0,
        )
        raise GitHubActionsError(
            f"could not launch GitHub Actions monitor supervisor: {message}"
        ) from error
    launch = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "GITHUB_ACTIONS_MONITOR_SUPERVISOR_LAUNCH",
        "monitor_id": resolved_monitor_id,
        "supervisor_pid": int(process.pid),
        "launched_at": core.utc_now(),
    }
    launched_identity = worker_lease.process_identity(int(process.pid))
    if launched_identity is not None:
        launch["supervisor_identity"] = launched_identity
    launch_recorded = False
    with contextlib.suppress(OSError):
        core.atomic_json(supervisor_launch_path(directory), launch)
        launch_recorded = True
    return {
        **descriptor,
        "supervisor_pid": int(process.pid),
        "supervisor_launch_recorded": launch_recorded,
        "descriptor_path": str(descriptor_path),
        "idempotent": False,
    }


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def bounded_tail(data: bytes, *, limit: int = MAX_CAPTURE_BYTES) -> str:
    tail = data[-limit:]
    redacted = redact_text(tail.decode("utf-8", errors="replace"))
    encoded = redacted.encode("utf-8")
    if len(encoded) <= limit:
        return redacted
    return encoded[-limit:].decode("utf-8", errors="ignore")


def command_capture(data: bytes, *, tail: bool = True) -> dict[str, Any]:
    return {
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        **({"tail": bounded_tail(data)} if tail and data else {}),
    }


class _BoundedStreamCapture:
    def __init__(self, *, limit: int = MAX_CAPTURE_BYTES) -> None:
        self.limit = limit
        self.total = 0
        self.digest = hashlib.sha256()
        self.tail = bytearray()

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.digest.update(chunk)
        self.tail.extend(chunk)
        if len(self.tail) > self.limit:
            del self.tail[: len(self.tail) - self.limit]

    def result(self, *, include_tail: bool = True) -> dict[str, Any]:
        return {
            "size_bytes": self.total,
            "sha256": self.digest.hexdigest(),
            **(
                {"tail": bounded_tail(bytes(self.tail), limit=self.limit)}
                if include_tail and self.tail
                else {}
            ),
        }

    def tail_bytes(self) -> bytes:
        return bytes(self.tail)


def _drain_stream(stream: Any, capture: _BoundedStreamCapture) -> None:
    while True:
        chunk = stream.read(8192)
        if not chunk:
            return
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        capture.add(chunk)


def classify_cli_error(stderr: str) -> str:
    lowered = stderr.casefold()
    if any(marker in lowered for marker in AUTH_MARKERS):
        return "authentication_failed"
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        return "run_not_found"
    if any(marker in lowered for marker in NETWORK_MARKERS):
        return "network_failure"
    return "gh_command_failed"


def run_bounded_command(
    command: list[str],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    stdout_capture = _BoundedStreamCapture(limit=MAX_VIEW_BYTES)
    stderr_capture = _BoundedStreamCapture(limit=MAX_CAPTURE_BYTES)
    drains: list[threading.Thread] = []
    for stream, capture in (
        (process.stdout, stdout_capture),
        (process.stderr, stderr_capture),
    ):
        if stream is None:
            continue
        drain = threading.Thread(
            target=_drain_stream,
            args=(stream, capture),
            daemon=True,
        )
        drain.start()
        drains.append(drain)
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(process)
    for drain in drains:
        drain.join(timeout=TERMINATION_GRACE_SECONDS)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    return {
        "returncode": process.poll(),
        "timed_out": timed_out,
        "stdout_bytes": stdout_capture.tail_bytes(),
        "stderr_bytes": stderr_capture.tail_bytes(),
        "stdout": stdout_capture.result(include_tail=False),
        "stderr": stderr_capture.result(),
    }


def run_view(
    descriptor: dict[str, Any],
    *,
    runner=None,
) -> dict[str, Any]:
    command = [
        str(descriptor["gh_command"]),
        "run",
        "view",
        str(descriptor["run_id"]),
        "--repo",
        repo_argument(str(descriptor["hostname"]), str(descriptor["repository"])),
        "--json",
        VIEW_FIELDS,
    ]
    if descriptor.get("attempt") is not None:
        command.extend(["--attempt", str(descriptor["attempt"])])
    started = time.monotonic()
    if runner is None:
        try:
            bounded = run_bounded_command(
                command,
                timeout_seconds=VIEW_TIMEOUT_SECONDS,
            )
        except OSError as error:
            data = str(error).encode("utf-8", errors="replace")
            return {
                "ok": False,
                "failure_kind": "gh_command_failed",
                "exit_code": None,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": command_capture(b"", tail=False),
                "stderr": command_capture(data),
            }
        evidence = {
            "exit_code": (
                int(bounded["returncode"])
                if bounded["returncode"] is not None
                else None
            ),
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": bounded["stdout"],
            "stderr": bounded["stderr"],
        }
        if bounded["timed_out"]:
            return {**evidence, "ok": False, "failure_kind": "view_timeout"}
        if bounded["stdout"]["size_bytes"] > MAX_VIEW_BYTES:
            return {
                **evidence,
                "ok": False,
                "failure_kind": "view_output_too_large",
            }
        stdout = bounded["stdout_bytes"]
        stderr = bounded["stderr_bytes"]
        return _parse_view_result(
            stdout=stdout,
            stderr=stderr,
            evidence=evidence,
        )
    try:
        completed = runner(
            command,
            capture_output=True,
            check=False,
            timeout=VIEW_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        return {
            "ok": False,
            "failure_kind": "view_timeout",
            "exit_code": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": command_capture(stdout, tail=False),
            "stderr": command_capture(stderr),
        }
    except OSError as error:
        data = str(error).encode("utf-8", errors="replace")
        return {
            "ok": False,
            "failure_kind": "gh_command_failed",
            "exit_code": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": command_capture(b"", tail=False),
            "stderr": command_capture(data),
        }
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    if isinstance(stdout, str):
        stdout = stdout.encode()
    if isinstance(stderr, str):
        stderr = stderr.encode()
    evidence: dict[str, Any] = {
        "exit_code": int(completed.returncode),
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": command_capture(stdout, tail=False),
        "stderr": command_capture(stderr),
    }
    if len(stdout) > MAX_VIEW_BYTES:
        return {**evidence, "ok": False, "failure_kind": "view_output_too_large"}
    return _parse_view_result(stdout=stdout, stderr=stderr, evidence=evidence)


def _parse_view_result(
    *,
    stdout: bytes,
    stderr: bytes,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    exit_code = evidence.get("exit_code")
    if exit_code != 0:
        return {
            **evidence,
            "ok": False,
            "failure_kind": classify_cli_error(bounded_tail(stderr)),
        }
    try:
        view = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**evidence, "ok": False, "failure_kind": "invalid_view_json"}
    if not isinstance(view, dict):
        return {**evidence, "ok": False, "failure_kind": "invalid_view_json"}
    return {**evidence, "ok": True, "view": view}


def _diagnostic_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(redact_text(value).split())
    return normalized[:MAX_DIAGNOSTIC_NAME_LENGTH] or None


def summarize_problem_jobs(value: object) -> dict[str, Any]:
    jobs = value.get("jobs") if isinstance(value, dict) else None
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise GitHubActionsError("jobs output must contain an array of objects")
    problem_jobs: list[dict[str, Any]] = []
    problem_job_count = 0
    problem_step_count = 0
    retained_step_count = 0
    truncated = False
    for job in jobs:
        conclusion = job.get("conclusion")
        if conclusion not in PROBLEM_CONCLUSIONS:
            continue
        problem_job_count += 1
        retain_job = len(problem_jobs) < MAX_PROBLEM_JOBS
        if not retain_job:
            truncated = True
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            steps = []
        failed_steps: list[dict[str, Any]] = []
        for step in steps:
            if (
                not isinstance(step, dict)
                or step.get("conclusion") not in PROBLEM_CONCLUSIONS
            ):
                continue
            problem_step_count += 1
            if not retain_job or retained_step_count >= MAX_PROBLEM_STEPS:
                truncated = True
                continue
            summary: dict[str, Any] = {
                "name": _diagnostic_name(step.get("name")) or "unknown",
                "status": _diagnostic_name(step.get("status")),
                "conclusion": step.get("conclusion"),
            }
            number = step.get("number")
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                summary["number"] = number
            failed_steps.append(summary)
            retained_step_count += 1
        if not retain_job:
            continue
        summary = {
            "name": _diagnostic_name(job.get("name")) or "unknown",
            "status": _diagnostic_name(job.get("status")),
            "conclusion": conclusion,
            "problem_steps": failed_steps,
        }
        database_id = job.get("databaseId")
        if (
            isinstance(database_id, int)
            and not isinstance(database_id, bool)
            and database_id > 0
        ):
            summary["database_id"] = database_id
        problem_jobs.append(summary)
    return {
        "job_count": len(jobs),
        "problem_job_count": problem_job_count,
        "problem_step_count": problem_step_count,
        "truncated": truncated,
        "problem_jobs": problem_jobs,
    }


def run_failure_diagnostics(
    descriptor: dict[str, Any],
    *,
    runner=None,
) -> dict[str, Any]:
    command = [
        str(descriptor["gh_command"]),
        "run",
        "view",
        str(descriptor["run_id"]),
        "--repo",
        repo_argument(str(descriptor["hostname"]), str(descriptor["repository"])),
        "--json",
        JOBS_FIELDS,
    ]
    if descriptor.get("attempt") is not None:
        command.extend(["--attempt", str(descriptor["attempt"])])
    started = time.monotonic()
    stdout_metadata: dict[str, Any] | None = None
    stderr_metadata: dict[str, Any] | None = None
    try:
        if runner is None:
            bounded = run_bounded_command(command, timeout_seconds=VIEW_TIMEOUT_SECONDS)
            stdout = bounded["stdout_bytes"]
            stderr = bounded["stderr_bytes"]
            exit_code = bounded["returncode"]
            timed_out = bounded["timed_out"]
            stdout_metadata = dict(bounded["stdout"])
            stderr_metadata = dict(bounded["stderr"])
        else:
            completed = runner(
                command,
                capture_output=True,
                check=False,
                timeout=VIEW_TIMEOUT_SECONDS,
            )
            stdout = completed.stdout or b""
            stderr = completed.stderr or b""
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
            exit_code = int(completed.returncode)
            timed_out = False
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        exit_code = None
        timed_out = True
    except OSError as error:
        stdout = b""
        stderr = str(error).encode("utf-8", errors="replace")
        exit_code = None
        timed_out = False
    command_result = {
        "exit_code": int(exit_code) if exit_code is not None else None,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout_metadata or command_capture(stdout, tail=False),
        "stderr": stderr_metadata or command_capture(stderr, tail=exit_code != 0),
    }
    if exit_code == 0:
        command_result["stderr"].pop("tail", None)
    if timed_out:
        return {
            "status": "unavailable",
            "failure_kind": "diagnostics_timeout",
            "command": command_result,
        }
    if exit_code != 0:
        return {
            "status": "unavailable",
            "failure_kind": classify_cli_error(bounded_tail(stderr)),
            "command": command_result,
        }
    if command_result["stdout"]["size_bytes"] > MAX_VIEW_BYTES:
        return {
            "status": "unavailable",
            "failure_kind": "diagnostics_output_too_large",
            "command": command_result,
        }
    try:
        payload = json.loads(stdout.decode("utf-8"))
        summary = summarize_problem_jobs(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, GitHubActionsError):
        return {
            "status": "unavailable",
            "failure_kind": "invalid_diagnostics_json",
            "command": command_result,
        }
    return {
        "status": "available",
        **summary,
        "command": command_result,
    }


def run_list(
    descriptor: dict[str, Any],
    *,
    runner=None,
) -> dict[str, Any]:
    command = [
        str(descriptor["gh_command"]),
        "run",
        "list",
        "--repo",
        repo_argument(str(descriptor["hostname"]), str(descriptor["repository"])),
        "--limit",
        "100",
        "--json",
        LIST_FIELDS,
    ]
    started = time.monotonic()
    if runner is None:
        try:
            bounded = run_bounded_command(
                command,
                timeout_seconds=VIEW_TIMEOUT_SECONDS,
            )
        except OSError as error:
            data = str(error).encode("utf-8", errors="replace")
            return {
                "ok": False,
                "failure_kind": "gh_command_failed",
                "exit_code": None,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": command_capture(b"", tail=False),
                "stderr": command_capture(data),
            }
        evidence = {
            "exit_code": (
                int(bounded["returncode"])
                if bounded["returncode"] is not None
                else None
            ),
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": bounded["stdout"],
            "stderr": bounded["stderr"],
        }
        if bounded["timed_out"]:
            return {**evidence, "ok": False, "failure_kind": "view_timeout"}
        if bounded["stdout"]["size_bytes"] > MAX_VIEW_BYTES:
            return {
                **evidence,
                "ok": False,
                "failure_kind": "view_output_too_large",
            }
        return _parse_list_result(
            stdout=bounded["stdout_bytes"],
            stderr=bounded["stderr_bytes"],
            evidence=evidence,
        )
    try:
        completed = runner(
            command,
            capture_output=True,
            check=False,
            timeout=VIEW_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        return {
            "ok": False,
            "failure_kind": "view_timeout",
            "exit_code": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": command_capture(stdout, tail=False),
            "stderr": command_capture(stderr),
        }
    except OSError as error:
        data = str(error).encode("utf-8", errors="replace")
        return {
            "ok": False,
            "failure_kind": "gh_command_failed",
            "exit_code": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": command_capture(b"", tail=False),
            "stderr": command_capture(data),
        }
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    if isinstance(stdout, str):
        stdout = stdout.encode()
    if isinstance(stderr, str):
        stderr = stderr.encode()
    evidence = {
        "exit_code": int(completed.returncode),
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": command_capture(stdout, tail=False),
        "stderr": command_capture(stderr),
    }
    if len(stdout) > MAX_VIEW_BYTES:
        return {**evidence, "ok": False, "failure_kind": "view_output_too_large"}
    return _parse_list_result(stdout=stdout, stderr=stderr, evidence=evidence)


def _parse_list_result(
    *,
    stdout: bytes,
    stderr: bytes,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if evidence.get("exit_code") != 0:
        return {
            **evidence,
            "ok": False,
            "failure_kind": classify_cli_error(bounded_tail(stderr)),
        }
    try:
        runs = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**evidence, "ok": False, "failure_kind": "invalid_view_json"}
    if not isinstance(runs, list) or any(not isinstance(item, dict) for item in runs):
        return {**evidence, "ok": False, "failure_kind": "invalid_view_json"}
    return {**evidence, "ok": True, "runs": runs}


def discover_run(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
    list_runner=None,
    sleep=time.sleep,
) -> dict[str, Any]:
    directory = Path(str(descriptor["monitor_dir"]))
    expected_sha = str(descriptor["expected_head_sha"])
    workflow_name = descriptor.get("workflow_name")
    timeout_seconds = float(descriptor["timeout_seconds"])
    started = time.monotonic()
    deadline = started + timeout_seconds
    query_count = 0
    last_query: dict[str, Any] | None = None
    last_heartbeat = started
    while True:
        if (directory / "cancel-request.json").is_file():
            return {
                "status": "cancelled",
                "failure_kind": "operator_cancelled",
                "query_count": query_count,
                "duration_seconds": round(time.monotonic() - started, 3),
                "last_query": last_query,
            }
        query = run_list(descriptor, runner=list_runner)
        query_count += 1
        last_query = {key: value for key, value in query.items() if key != "runs"}
        if not query.get("ok"):
            return {
                "status": "unavailable",
                "failure_kind": query.get("failure_kind"),
                "query_count": query_count,
                "duration_seconds": round(time.monotonic() - started, 3),
                "last_query": last_query,
            }
        matching: dict[int, dict[str, Any]] = {}
        identity_error = None
        for candidate in query["runs"]:
            head_sha = candidate.get("headSha")
            if not isinstance(head_sha, str) or head_sha.lower() != expected_sha:
                continue
            if (
                workflow_name is not None
                and candidate.get("workflowName") != workflow_name
            ):
                continue
            if not repository_url_matches(
                candidate.get("url"),
                hostname=str(descriptor["hostname"]),
                repository=str(descriptor["repository"]),
            ):
                identity_error = "repository_url_mismatch"
                continue
            candidate_id = candidate.get("databaseId")
            if isinstance(candidate_id, int) and candidate_id > 0:
                matching[candidate_id] = candidate
            else:
                identity_error = "run_id_mismatch"
        if identity_error is not None:
            return {
                "status": "ambiguous",
                "failure_kind": identity_error,
                "query_count": query_count,
                "candidate_count": len(matching),
                "duration_seconds": round(time.monotonic() - started, 3),
                "last_query": last_query,
            }
        if len(matching) > 1:
            return {
                "status": "ambiguous",
                "failure_kind": "multiple_matching_runs",
                "query_count": query_count,
                "candidate_count": len(matching),
                "duration_seconds": round(time.monotonic() - started, 3),
                "last_query": last_query,
            }
        if matching:
            run_id, selected = next(iter(matching.items()))
            return {
                "status": "resolved",
                "run_id": run_id,
                "workflow_name": selected.get("workflowName"),
                "query_count": query_count,
                "candidate_count": 1,
                "duration_seconds": round(time.monotonic() - started, 3),
                "last_query": last_query,
            }
        now = time.monotonic()
        if now >= deadline:
            return {
                "status": "timed_out",
                "failure_kind": "run_discovery_timeout",
                "query_count": query_count,
                "candidate_count": 0,
                "duration_seconds": round(now - started, 3),
                "last_query": last_query,
            }
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            current = core.load_object(directory / "monitor.json")
            current["last_alive_at"] = core.utc_now()
            current["discovery_query_count"] = query_count
            core.atomic_json(directory / "monitor.json", current)
            last_heartbeat = now
        sleep(min(DISCOVERY_POLL_SECONDS, deadline - now))


def claim_discovered_run(
    project_root: Path,
    descriptor: dict[str, Any],
    discovery: dict[str, Any],
    *,
    state_dir: str,
) -> tuple[dict[str, Any], str | None]:
    """Persist one discovered run while preventing duplicate active ownership."""

    run_id = int(discovery["run_id"])
    descriptor_path = Path(str(descriptor["monitor_dir"])) / "monitor.json"
    owner = None
    with monitor_admission_lock(project_root, state_dir=state_dir):
        current = core.load_object(descriptor_path)
        for existing_path in sorted(
            monitor_root(project_root, state_dir=state_dir).glob("*/monitor.json")
        ):
            if existing_path == descriptor_path:
                continue
            existing = core.load_object(existing_path)
            if existing.get("status") in TERMINAL_MONITOR_STATUSES:
                continue
            if (
                existing.get("hostname") == current.get("hostname")
                and same_repository(
                    existing.get("repository"), current.get("repository")
                )
                and existing.get("run_id") == run_id
            ):
                owner = str(existing.get("monitor_id") or existing_path.parent.name)
                break
        if owner is None:
            current["run_id"] = run_id
            current["discovery"] = discovery
            current["discovery_query_count"] = int(discovery["query_count"])
            current["last_alive_at"] = core.utc_now()
            core.atomic_json(descriptor_path, current)
    return current, owner


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    try:
        group = os.getpgid(process.pid)
    except OSError:
        group = None
    try:
        if group == process.pid:
            os.killpg(group, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if group == process.pid:
            os.killpg(group, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_watch(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    directory = Path(str(descriptor["monitor_dir"]))
    log_path = directory / "full.log"
    command = [
        str(descriptor["gh_command"]),
        "run",
        "watch",
        str(descriptor["run_id"]),
        "--repo",
        repo_argument(str(descriptor["hostname"]), str(descriptor["repository"])),
        "--compact",
        "--exit-status",
        "--interval",
        "10",
    ]
    started = time.monotonic()
    deadline = (
        started + float(descriptor["timeout_seconds"])
        if descriptor.get("timeout_seconds") is not None
        else None
    )
    outcome = "exited"
    process = popen_factory(
        command,
        cwd=str(project_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    current = core.load_object(directory / "monitor.json")
    if current.get("status") not in TERMINAL_MONITOR_STATUSES:
        current["watch_pid"] = int(process.pid)
        with contextlib.suppress(OSError):
            current["watch_pgid"] = os.getpgid(process.pid)
        watch_identity = worker_lease.process_identity(int(process.pid))
        if watch_identity is not None:
            current["watch_identity"] = watch_identity
        core.atomic_json(directory / "monitor.json", current)
    capture = _BoundedStreamCapture()
    drain = None
    if process.stderr is not None:
        drain = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, capture),
            daemon=True,
        )
        drain.start()
    last_heartbeat = started
    while process.poll() is None:
        now = time.monotonic()
        if (directory / "cancel-request.json").is_file():
            outcome = "cancelled"
            _terminate_process(process)
            break
        if deadline is not None and now >= deadline:
            outcome = "timed_out"
            _terminate_process(process)
            break
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            current = core.load_object(directory / "monitor.json")
            current["last_alive_at"] = core.utc_now()
            core.atomic_json(directory / "monitor.json", current)
            last_heartbeat = now
        time.sleep(CONTROL_POLL_SECONDS)
    returncode = process.poll()
    if drain is not None:
        drain.join(timeout=TERMINATION_GRACE_SECONDS)
    if process.stderr is not None:
        process.stderr.close()
    captured = capture.result()
    captured_tail = str(captured.get("tail") or "")
    _write_text(log_path, captured_tail)
    failure_kind = (
        classify_cli_error(captured_tail)
        if outcome == "exited" and returncode not in {0, None}
        else None
    )
    return {
        "outcome": outcome,
        "exit_code": int(returncode) if returncode is not None else None,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output": captured,
        "log_path": str(log_path),
        "failure_kind": failure_kind,
    }


def validate_view_identity(
    descriptor: dict[str, Any],
    view: dict[str, Any],
) -> str | None:
    if not repository_url_matches(
        view.get("url"),
        hostname=str(descriptor.get("hostname", "")),
        repository=str(descriptor.get("repository", "")),
    ):
        return "repository_url_mismatch"
    if view.get("databaseId") != descriptor.get("run_id"):
        return "run_id_mismatch"
    attempt = view.get("attempt")
    if descriptor.get("attempt") is not None and attempt != descriptor.get("attempt"):
        return "run_attempt_mismatch"
    head_sha = view.get("headSha")
    expected = descriptor.get("expected_head_sha")
    if isinstance(expected, str) and (
        not isinstance(head_sha, str) or not head_sha.lower().startswith(expected)
    ):
        return "head_sha_mismatch"
    return None


def observe_run(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
    view_runner=None,
    watch_popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    initial = run_view(descriptor, runner=view_runner)
    watch: dict[str, Any] | None = None
    if not initial.get("ok"):
        return {
            "monitor_status": "unavailable",
            "failure_kind": initial.get("failure_kind"),
            "initial_view": initial,
            "final_view": initial,
            "watch": None,
        }
    initial_view = initial["view"]
    identity_error = validate_view_identity(descriptor, initial_view)
    if identity_error is not None:
        return {
            "monitor_status": "ambiguous",
            "failure_kind": identity_error,
            "initial_view": initial,
            "final_view": initial,
            "watch": None,
        }
    final = initial
    if initial_view.get("status") != TERMINAL_GITHUB_STATUS:
        watch = run_watch(
            project_root,
            descriptor,
            state_dir=state_dir,
            popen_factory=watch_popen_factory,
        )
        final = run_view(descriptor, runner=view_runner)
    if final.get("ok"):
        final_data = final["view"]
        identity_error = validate_view_identity(descriptor, final_data)
        if identity_error is not None:
            return {
                "monitor_status": "ambiguous",
                "failure_kind": identity_error,
                "initial_view": initial,
                "final_view": final,
                "watch": watch,
            }
        if final_data.get("status") == TERMINAL_GITHUB_STATUS:
            return {
                "monitor_status": "completed",
                "ci_conclusion": final_data.get("conclusion") or "ambiguous",
                "initial_view": initial,
                "final_view": final,
                "watch": watch,
            }
    watch_outcome = watch.get("outcome") if isinstance(watch, dict) else None
    if watch_outcome == "cancelled":
        monitor_status = "cancelled"
    elif watch_outcome == "timed_out":
        monitor_status = "timed_out"
    elif not final.get("ok"):
        monitor_status = "unavailable"
    else:
        watch_failure = watch.get("failure_kind") if isinstance(watch, dict) else None
        monitor_status = (
            "unavailable"
            if watch_failure
            in {"authentication_failed", "run_not_found", "network_failure"}
            else "ambiguous"
        )
    return {
        "monitor_status": monitor_status,
        "failure_kind": (
            final.get("failure_kind")
            or (watch.get("failure_kind") if isinstance(watch, dict) else None)
            or "github_state_incomplete"
        ),
        "initial_view": initial,
        "final_view": final,
        "watch": watch,
    }


def verification_status(observation: dict[str, Any]) -> str:
    if observation.get("monitor_status") != "completed":
        if observation.get("monitor_status") == "cancelled":
            return "cancelled"
        return "unknown"
    conclusion = observation.get("ci_conclusion")
    if conclusion == "success":
        return "passed"
    if conclusion in {"failure", "startup_failure", "timed_out", "action_required"}:
        return "failed"
    if conclusion == "cancelled":
        return "cancelled"
    return "unknown"


def event_status(observation: dict[str, Any]) -> str:
    monitor_status = str(observation.get("monitor_status"))
    if monitor_status != "completed":
        return (
            monitor_status
            if monitor_status in core.FOLLOWUP_TERMINAL_STATUSES
            else "failed"
        )
    conclusion = observation.get("ci_conclusion")
    if conclusion == "success":
        return "completed"
    if conclusion in {"failure", "startup_failure"}:
        return "failed"
    if conclusion in {"cancelled", "timed_out", "action_required"}:
        return str(conclusion)
    return "ambiguous"


def should_wake(wake_policy: str, observation: dict[str, Any]) -> bool:
    if wake_policy == "always":
        return True
    status = verification_status(observation)
    if wake_policy == "on-failure":
        return status != "passed"
    return status in {"failed", "cancelled", "unknown"}


def finalize_monitor(
    project_root: Path,
    descriptor: dict[str, Any],
    observation: dict[str, Any],
    *,
    state_dir: str,
    started_at: str,
    duration_seconds: float,
) -> dict[str, Any]:
    monitor_id = str(descriptor["monitor_id"])
    directory = Path(str(descriptor["monitor_dir"]))
    check_dir = (
        core.state_root(project_root, state_dir=state_dir) / "checks" / monitor_id
    )
    check_dir.mkdir(parents=True, exist_ok=True)
    log_path = directory / "full.log"
    if not log_path.exists():
        _write_text(log_path, "")
    final_view_record = observation.get("final_view")
    final_view = (
        final_view_record.get("view")
        if isinstance(final_view_record, dict) and final_view_record.get("ok")
        else None
    )
    status = verification_status(observation)
    summary = (
        f"GitHub Actions {descriptor['repository']} "
        f"run {descriptor.get('run_id') or '?'} "
        "attempt "
        f"{(final_view or {}).get('attempt') or descriptor.get('attempt') or '?'}: "
        f"monitor={observation.get('monitor_status')} "
        f"conclusion={observation.get('ci_conclusion') or 'unavailable'}"
    )
    diagnostics = observation.get("failure_diagnostics")
    problem_jobs = (
        diagnostics.get("problem_jobs")
        if isinstance(diagnostics, dict)
        and diagnostics.get("status") == "available"
        else None
    )
    if isinstance(problem_jobs, list) and problem_jobs:
        first_job = problem_jobs[0]
        if isinstance(first_job, dict):
            problem = str(first_job.get("name") or "unknown")
            problem_steps = first_job.get("problem_steps")
            if isinstance(problem_steps, list) and problem_steps:
                first_step = problem_steps[0]
                if isinstance(first_step, dict):
                    problem += f" / {first_step.get('name') or 'unknown'}"
            summary += f" problem={problem}"
    summary_path = check_dir / "summary.txt"
    _write_text(summary_path, summary + "\n")
    check_log = check_dir / "full.log"
    _write_text(check_log, log_path.read_text(encoding="utf-8", errors="replace"))
    finished_at = core.utc_now()
    result_path = check_dir / "verification-result.json"
    result = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
        "check_id": monitor_id,
        "status": status,
        "suite": "github-actions",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 3),
        "result_path": str(result_path),
        "summary_path": str(summary_path),
        "log_path": str(check_log),
        "commands": [],
        "github_actions": {
            "hostname": descriptor["hostname"],
            "repository": descriptor["repository"],
            "run_id": descriptor.get("run_id"),
            "attempt": (final_view or {}).get("attempt") or descriptor.get("attempt"),
            "head_sha": (final_view or {}).get("headSha"),
            "workflow_name": (final_view or {}).get("workflowName"),
            "event": (final_view or {}).get("event"),
            "status": (final_view or {}).get("status"),
            "conclusion": observation.get("ci_conclusion"),
            "url": (final_view or {}).get("url"),
            "monitor_status": observation.get("monitor_status"),
            "failure_kind": observation.get("failure_kind"),
        },
    }
    if isinstance(observation.get("discovery"), dict):
        result["github_actions"]["discovery"] = observation["discovery"]
    if isinstance(observation.get("error"), str):
        result["github_actions"]["error"] = redact_text(
            observation["error"]
        )[:MAX_REASON_LENGTH]
    if isinstance(observation.get("failure_diagnostics"), dict):
        result["github_actions"]["failure_diagnostics"] = observation[
            "failure_diagnostics"
        ]
    core.atomic_json(result_path, result)
    evidence_path = directory / "evidence.json"
    evidence = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "monitor_id": monitor_id,
        "source_kind": SOURCE_KIND,
        "hostname": descriptor["hostname"],
        "repository": descriptor["repository"],
        "run_id": descriptor.get("run_id"),
        "requested_attempt": descriptor.get("attempt"),
        "expected_head_sha": descriptor.get("expected_head_sha"),
        "monitor_status": observation.get("monitor_status"),
        "ci_conclusion": observation.get("ci_conclusion"),
        "failure_kind": observation.get("failure_kind"),
        "initial_view": observation.get("initial_view"),
        "watch": observation.get("watch"),
        "final_view": observation.get("final_view"),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 3),
        "summary": summary,
        "result_path": str(result_path),
        "summary_path": str(summary_path),
        "log_path": str(log_path),
    }
    if isinstance(observation.get("discovery"), dict):
        evidence["discovery"] = observation["discovery"]
    if isinstance(observation.get("error"), str):
        evidence["error"] = redact_text(observation["error"])[
            :MAX_REASON_LENGTH
        ]
    if isinstance(observation.get("recovery"), dict):
        evidence["recovery"] = observation["recovery"]
    if isinstance(observation.get("failure_diagnostics"), dict):
        evidence["failure_diagnostics"] = observation["failure_diagnostics"]
    if isinstance(descriptor.get("wake_target"), dict):
        evidence["wake_target"] = descriptor["wake_target"]
    core.atomic_json(evidence_path, evidence)
    terminal_status = event_status(observation)
    emitted = core.write_followup_event(
        project_root,
        operation_id=monitor_id,
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
        emit_signal=should_wake(str(descriptor["wake_policy"]), observation),
    )
    final_descriptor = {
        **descriptor,
        "status": observation.get("monitor_status"),
        "ci_conclusion": observation.get("ci_conclusion"),
        "failure_kind": observation.get("failure_kind"),
        "started_at": started_at,
        "finished_at": finished_at,
        "last_alive_at": finished_at,
        "result_path": str(result_path),
        "evidence_path": str(evidence_path),
        "summary_path": str(summary_path),
        "event_path": emitted["event_path"],
        "signal_path": emitted["signal_path"],
        "signal_emitted": emitted["signal_emitted"],
    }
    if isinstance(observation.get("discovery"), dict):
        final_descriptor["discovery"] = observation["discovery"]
        final_descriptor["discovery_query_count"] = int(
            observation["discovery"].get("query_count", 0)
        )
    if isinstance(observation.get("failure_diagnostics"), dict):
        diagnostics = observation["failure_diagnostics"]
        final_descriptor["failure_diagnostics"] = {
            key: diagnostics[key]
            for key in (
                "status",
                "failure_kind",
                "job_count",
                "problem_job_count",
                "problem_step_count",
                "truncated",
            )
            if key in diagnostics
        }
    core.atomic_json(directory / "monitor.json", final_descriptor)
    return final_descriptor


def supervise_monitor(
    project_root: Path,
    *,
    monitor_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    view_runner=None,
    list_runner=None,
    diagnostics_runner=None,
    watch_popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    directory = monitor_dir_for(project, monitor_id, state_dir=state_dir)
    descriptor_path = directory / "monitor.json"
    supervisor_identity = worker_lease.process_identity(os.getpid())
    if supervisor_identity is None:
        raise GitHubActionsError(
            "cannot record the detached monitor supervisor process identity"
        )
    with monitor_admission_lock(project, state_dir=state_dir):
        descriptor = core.load_object(descriptor_path)
        if descriptor.get("kind") != MONITOR_KIND:
            raise GitHubActionsError("monitor descriptor has unexpected kind")
        if descriptor.get("status") in TERMINAL_MONITOR_STATUSES:
            return descriptor
        if descriptor.get("status") == "running":
            existing_state = supervisor_process_state(directory, descriptor)
            if existing_state["state"] == "gone":
                raise GitHubActionsError(
                    "monitor supervisor is gone; run ci reap before retrying"
                )
            raise GitHubActionsError("monitor is already owned by a supervisor")
        started_at = core.utc_now()
        descriptor.update(
            {
                "status": "running",
                "supervisor_pid": os.getpid(),
                "supervisor_identity": supervisor_identity,
                "started_at": started_at,
                "last_alive_at": started_at,
            }
        )
        core.atomic_json(descriptor_path, descriptor)
    started = time.monotonic()
    discovery = None
    try:
        runtime_descriptor = descriptor
        if descriptor.get("run_id") is None:
            discovery = discover_run(
                project,
                descriptor,
                state_dir=state_dir,
                list_runner=list_runner,
            )
            if discovery.get("status") != "resolved":
                observation = {
                    "monitor_status": discovery.get("status"),
                    "failure_kind": discovery.get("failure_kind"),
                    "initial_view": None,
                    "watch": None,
                    "final_view": None,
                    "discovery": discovery,
                }
                return finalize_monitor(
                    project,
                    descriptor,
                    observation,
                    state_dir=state_dir,
                    started_at=started_at,
                    duration_seconds=time.monotonic() - started,
                )
            descriptor, active_owner = claim_discovered_run(
                project,
                descriptor,
                discovery,
                state_dir=state_dir,
            )
            if active_owner is not None:
                discovery["active_monitor_id"] = active_owner
                observation = {
                    "monitor_status": "ambiguous",
                    "failure_kind": "run_already_monitored",
                    "initial_view": None,
                    "watch": None,
                    "final_view": None,
                    "discovery": discovery,
                }
                return finalize_monitor(
                    project,
                    descriptor,
                    observation,
                    state_dir=state_dir,
                    started_at=started_at,
                    duration_seconds=time.monotonic() - started,
                )
            runtime_descriptor = dict(descriptor)
            if descriptor.get("timeout_seconds") is not None:
                remaining = float(descriptor["timeout_seconds"]) - (
                    time.monotonic() - started
                )
                if remaining <= 0:
                    observation = {
                        "monitor_status": "timed_out",
                        "failure_kind": "run_discovery_timeout",
                        "initial_view": None,
                        "watch": None,
                        "final_view": None,
                        "discovery": discovery,
                    }
                    return finalize_monitor(
                        project,
                        descriptor,
                        observation,
                        state_dir=state_dir,
                        started_at=started_at,
                        duration_seconds=time.monotonic() - started,
                    )
                runtime_descriptor["timeout_seconds"] = remaining
        observation = observe_run(
            project,
            runtime_descriptor,
            state_dir=state_dir,
            view_runner=view_runner,
            watch_popen_factory=watch_popen_factory,
        )
        if (
            observation.get("monitor_status") == "completed"
            and observation.get("ci_conclusion") in DIAGNOSTIC_CONCLUSIONS
        ):
            try:
                observation["failure_diagnostics"] = run_failure_diagnostics(
                    runtime_descriptor,
                    runner=diagnostics_runner,
                )
            except Exception as error:
                observation["failure_diagnostics"] = {
                    "status": "unavailable",
                    "failure_kind": "diagnostics_process_failure",
                    "error": redact_text(str(error))[:MAX_REASON_LENGTH],
                    "command": {},
                }
        if discovery is not None:
            observation["discovery"] = discovery
    except Exception as error:  # supervisor must always leave terminal evidence
        observation = {
            "monitor_status": "failed",
            "failure_kind": "monitor_process_failure",
            "error": redact_text(str(error))[:1000],
            "initial_view": None,
            "watch": None,
            "final_view": None,
        }
        if isinstance(discovery, dict):
            observation["discovery"] = discovery
    return finalize_monitor(
        project,
        descriptor,
        observation,
        state_dir=state_dir,
        started_at=started_at,
        duration_seconds=time.monotonic() - started,
    )


def cancel_monitor(
    project_root: Path,
    *,
    monitor_id: str,
    reason: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    normalized_reason = bounded_reason(reason, field="cancel reason")
    directory = monitor_dir_for(project_root, monitor_id, state_dir=state_dir)
    descriptor = core.load_object(directory / "monitor.json")
    if descriptor.get("status") in TERMINAL_MONITOR_STATUSES:
        return {**descriptor, "cancel_requested": False, "idempotent": True}
    request = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "GITHUB_ACTIONS_MONITOR_CANCEL_REQUEST",
        "monitor_id": monitor_id,
        "reason": normalized_reason,
        "requested_at": core.utc_now(),
    }
    path = directory / "cancel-request.json"
    if path.exists():
        existing = core.load_object(path)
        return {**existing, "path": str(path), "idempotent": True}
    core.atomic_json(path, request)
    return {**request, "path": str(path), "idempotent": False}


def retry_monitor(
    project_root: Path,
    *,
    monitor_id: str,
    reason: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    normalized_reason = bounded_reason(reason, field="retry reason")
    project = project_root.expanduser().resolve()
    original_dir = monitor_dir_for(project, monitor_id, state_dir=state_dir)
    original = core.load_object(original_dir / "monitor.json")
    if original.get("status") not in {
        "failed",
        "cancelled",
        "timed_out",
        "unavailable",
        "ambiguous",
    }:
        raise GitHubActionsError(
            "only a terminal unsuccessful monitor can be retried"
        )
    retry_index = 1
    while True:
        new_id = f"{monitor_id}-r{retry_index}"
        if not (monitor_root(project, state_dir=state_dir) / new_id).exists():
            break
        retry_index += 1
    return start_monitor(
        project,
        repository=str(original["repository"]),
        run_id=original.get("requested_run_id", original.get("run_id")),
        state_dir=state_dir,
        hostname=str(original["hostname"]),
        attempt=original.get("attempt"),
        expected_head_sha=original.get("expected_head_sha"),
        workflow_name=original.get("workflow_name"),
        gh_command=None,
        timeout_seconds=original.get("timeout_seconds"),
        wake_policy=str(original["wake_policy"]),
        monitor_id=new_id,
        retry_of=monitor_id,
        retry_reason=normalized_reason,
        popen_factory=popen_factory,
    )


def supervisor_process_state(
    directory: Path,
    descriptor: dict[str, Any],
) -> dict[str, Any]:
    identity = descriptor.get("supervisor_identity")
    pid = descriptor.get("supervisor_pid")
    launch_path = supervisor_launch_path(directory)
    if not isinstance(identity, dict) and launch_path.is_file():
        with contextlib.suppress(OSError, core.OrchestratorError):
            launch = core.load_object(launch_path)
            identity = launch.get("supervisor_identity")
            pid = launch.get("supervisor_pid")
    if isinstance(identity, dict):
        return worker_lease.identity_state(identity)
    return worker_lease.pid_state(pid)


def reap_monitors(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Finalize active monitors whose supervisor is proven gone."""

    platform_runtime.require_detached_lifecycle("ci reap")
    project = project_root.expanduser().resolve()
    root = monitor_root(project, state_dir=state_dir)
    outcomes: list[dict[str, Any]] = []
    for descriptor_path in sorted(root.glob("*/monitor.json")):
        with monitor_admission_lock(project, state_dir=state_dir):
            try:
                descriptor = core.load_object(descriptor_path)
            except (OSError, core.OrchestratorError) as error:
                outcomes.append(
                    {
                        "monitor_id": descriptor_path.parent.name,
                        "status": "invalid",
                        "reason": redact_text(str(error))[:MAX_REASON_LENGTH],
                    }
                )
                continue
            if (
                not core.is_supported_schema_version(
                    descriptor.get("schema_version")
                )
                or descriptor.get("kind") != MONITOR_KIND
            ):
                outcomes.append(
                    {
                        "monitor_id": descriptor.get("monitor_id")
                        or descriptor_path.parent.name,
                        "status": "invalid",
                        "reason": "descriptor contract is invalid",
                    }
                )
                continue
            status = descriptor.get("status")
            monitor_id = descriptor.get("monitor_id")
            if status in TERMINAL_MONITOR_STATUSES:
                continue
            directory = descriptor_path.parent
            supervisor_state = supervisor_process_state(directory, descriptor)
            if supervisor_state["state"] == "alive":
                outcomes.append(
                    {"monitor_id": monitor_id, "status": "supervisor_alive"}
                )
                continue
            if supervisor_state["state"] == "unknown":
                outcomes.append(
                    {
                        "monitor_id": monitor_id,
                        "status": "unsafe_missing_identity",
                    }
                )
                continue
            failure_kind = (
                "supervisor_never_claimed"
                if status == "starting"
                else "supervisor_not_alive"
            )
            termination = worker_lease.stop_worker_tree(
                worker_pid=descriptor.get("watch_pid"),
                worker_pgid=descriptor.get("watch_pgid"),
                worker_identity=descriptor.get("watch_identity"),
                reason=failure_kind,
            )
            started_at = str(
                descriptor.get("started_at")
                or descriptor.get("created_at")
                or core.utc_now()
            )
            final = finalize_monitor(
                project,
                descriptor,
                {
                    "monitor_status": "failed",
                    "failure_kind": failure_kind,
                    "error": "detached monitor supervisor exited before finalization",
                    "recovery": {
                        "supervisor_identity_state": supervisor_state,
                        "watch_termination": termination,
                    },
                    "initial_view": None,
                    "watch": None,
                    "final_view": None,
                },
                state_dir=state_dir,
                started_at=started_at,
                duration_seconds=_timestamp_age_seconds(started_at),
            )
            outcomes.append(
                {
                    "monitor_id": monitor_id,
                    "status": "reaped",
                    "failure_kind": failure_kind,
                    "event_path": final.get("event_path"),
                }
            )
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "GITHUB_ACTIONS_MONITOR_REAP_REPORT",
        "project_root": str(project),
        "reaped_count": sum(item.get("status") == "reaped" for item in outcomes),
        "outcomes": outcomes,
    }


def monitor_status(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    monitor_id: str | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    root = monitor_root(project, state_dir=state_dir)
    paths = (
        [monitor_dir_for(project, monitor_id, state_dir=state_dir) / "monitor.json"]
        if monitor_id is not None
        else sorted(root.glob("*/monitor.json"))
    )
    monitors: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise GitHubActionsError(f"unknown monitor: {monitor_id}")
        try:
            descriptor = core.load_object(path)
        except (OSError, core.OrchestratorError) as error:
            if monitor_id is not None:
                raise GitHubActionsError(
                    f"monitor descriptor is unreadable: {path}: {error}"
                ) from error
            monitors.append(
                {
                    "monitor_id": path.parent.name,
                    "status": "invalid",
                    "failure_kind": "descriptor_unreadable",
                    "error": redact_text(str(error))[:MAX_REASON_LENGTH],
                    "suggested_action": (
                        "Inspect the descriptor without deleting durable monitor "
                        "artifacts; repair only from a known audit source."
                    ),
                }
            )
            continue
        if (
            not core.is_supported_schema_version(descriptor.get("schema_version"))
            or descriptor.get("kind") != MONITOR_KIND
        ):
            monitors.append(
                {
                    "monitor_id": descriptor.get("monitor_id") or path.parent.name,
                    "status": "invalid",
                    "failure_kind": "descriptor_contract_invalid",
                    "suggested_action": (
                        "Inspect the descriptor and run upgrade diagnostics; do not "
                        "rewrite or delete durable monitor artifacts blindly."
                    ),
                }
            )
            continue
        summary = {
            key: descriptor.get(key)
            for key in (
                "monitor_id",
                "status",
                "hostname",
                "repository",
                "run_id",
                "requested_run_id",
                "attempt",
                "expected_head_sha",
                "workflow_name",
                "discovery_query_count",
                "ci_conclusion",
                "failure_kind",
                "created_at",
                "started_at",
                "finished_at",
                "last_alive_at",
                "result_path",
                "evidence_path",
                "event_path",
                "signal_path",
                "signal_emitted",
            )
        }
        summary["phase"] = (
            "discovering"
            if descriptor.get("run_id") is None
            and descriptor.get("status") in {"starting", "running"}
            else "watching"
            if descriptor.get("status") in {"starting", "running"}
            else "terminal"
        )
        if descriptor.get("status") in {"starting", "running"}:
            process_state = supervisor_process_state(path.parent, descriptor)
            summary["supervisor_state"] = process_state["state"]
            summary["supervisor_identity_verified"] = process_state.get(
                "identity_verified", False
            )
            if process_state["state"] == "gone":
                summary["status"] = "crashed"
                summary["failure_kind"] = (
                    "supervisor_never_claimed"
                    if descriptor.get("status") == "starting"
                    else "supervisor_not_alive"
                )
                summary["suggested_action"] = (
                    "Run ci reap, inspect bounded evidence, then use ci retry "
                    "with an explicit reason if appropriate."
                )
            elif (
                process_state["state"] == "unknown"
                and _timestamp_age_seconds(descriptor.get("created_at"))
                > STARTING_GRACE_SECONDS
            ):
                summary["status"] = "stalled"
                summary["failure_kind"] = "supervisor_identity_unavailable"
                summary["suggested_action"] = (
                    "Inspect supervisor.log; the process cannot be reaped safely "
                    "without a recorded identity."
                )
        monitors.append(summary)
    counts: dict[str, int] = {}
    conclusions: dict[str, int] = {}
    for item in monitors:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        conclusion = item.get("ci_conclusion")
        if isinstance(conclusion, str):
            conclusions[conclusion] = conclusions.get(conclusion, 0) + 1
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": STATUS_KIND,
        "monitor_count": len(monitors),
        "status_counts": counts,
        "conclusion_counts": conclusions,
        "monitors": monitors,
        "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }


def _timestamp_age_seconds(value: object) -> float:
    if not isinstance(value, str):
        return float("inf")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds(), 0.0)
