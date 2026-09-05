"""Detached exact-PR readiness monitoring through an authenticated gh CLI."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    binding,
    core,
    github_actions,
    platform_runtime,
    verification,
    worker_lease,
)

SOURCE_KIND = "github_pull_request"
MONITOR_KIND = "GITHUB_PR_READINESS_MONITOR"
EVIDENCE_KIND = "GITHUB_PR_READINESS_EVIDENCE"
STATUS_KIND = "GITHUB_PR_READINESS_STATUS"
VIEW_FIELDS = (
    "number,state,isDraft,headRefOid,reviewDecision,mergeable,"
    "statusCheckRollup,url"
)
REVIEW_POLICIES = {"ignore", "approved"}
WAKE_POLICIES = github_actions.WAKE_POLICIES
TERMINAL_STATUSES = {
    "ready",
    "merged",
    "failed_checks",
    "changes_requested",
    "conflicting",
    "head_changed",
    "closed",
    "cancelled",
    "timed_out",
    "unavailable",
    "ambiguous",
    "failed",
}
SUCCESS_STATUSES = {"ready", "merged"}
ACTION_STATUSES = TERMINAL_STATUSES - SUCCESS_STATUSES - {"cancelled"}
CHECK_FAILURES = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "FAILURE",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
CHECK_SUCCESSES = {"SUCCESS", "NEUTRAL", "SKIPPED"}
STATUS_FAILURES = {"ERROR", "FAILURE"}
STATUS_SUCCESSES = {"SUCCESS"}
DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60
MAX_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
MAX_INTERVAL_SECONDS = 5 * 60
MAX_CHECKS = 256
MAX_CHECK_NAME = 256
MAX_FAILED_NAMES = 20
STARTING_GRACE_SECONDS = 30.0
MAX_REASON_LENGTH = github_actions.MAX_REASON_LENGTH
PR_NOT_FOUND_MARKERS = (
    "could not resolve to a pullrequest",
    "pull request not found",
    "no pull requests found",
    "http 404",
)


class GitHubPullRequestError(RuntimeError):
    """A deterministic pull-request monitor contract failure."""


def monitor_root(project_root: Path, *, state_dir: str) -> Path:
    return (
        core.state_root(project_root, state_dir=state_dir)
        / "monitors"
        / "github-pull-requests"
    )


def monitor_dir_for(
    project_root: Path,
    monitor_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return monitor_root(
        project_root, state_dir=state_dir
    ) / github_actions.validate_monitor_id(monitor_id)


@contextlib.contextmanager
def admission_lock(project_root: Path, *, state_dir: str):
    path = monitor_root(project_root, state_dir=state_dir) / ".admission.lock"
    with platform_runtime.exclusive_file_lock(path):
        yield


def default_monitor_id(
    *,
    hostname: str,
    repository: str,
    pr_number: int,
    expected_head_sha: str,
    review_policy: str,
) -> str:
    identity = (
        f"{hostname}/{repository.casefold()}/{pr_number}/{expected_head_sha}/"
        f"{review_policy}"
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"ghpr-{pr_number}-{suffix}"


def validate_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", normalized):
        raise GitHubPullRequestError(
            "expected-head-sha must be a full hexadecimal commit id"
        )
    return normalized


def bounded_setting(value: float, *, field: str, maximum: float) -> float:
    if isinstance(value, bool):
        raise GitHubPullRequestError(f"{field} must be between 0 and {maximum}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise GitHubPullRequestError(
            f"{field} must be between 0 and {maximum}"
        ) from error
    if not math.isfinite(parsed) or parsed <= 0 or parsed > maximum:
        raise GitHubPullRequestError(f"{field} must be between 0 and {maximum}")
    return parsed


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
        "pr",
        "supervise",
        "--monitor-id",
        monitor_id,
    ]


def launch_path(directory: Path) -> Path:
    return directory / "supervisor-launch.json"


def cancel_path(directory: Path) -> Path:
    return directory / "cancel-request.json"


def reap_process(process: subprocess.Popen[bytes]) -> None:
    process.wait()


def start_monitor(
    project_root: Path,
    *,
    repository: str,
    pr_number: int | str,
    expected_head_sha: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    hostname: str = "github.com",
    review_policy: str = "ignore",
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    wake_policy: str = "always",
    gh_command: str | None = None,
    monitor_id: str | None = None,
    retry_of: str | None = None,
    retry_reason: str | None = None,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    platform_runtime.require_detached_lifecycle("pr watch")
    project = project_root.expanduser().resolve()
    config = github_actions.load_config(project, state_dir=state_dir)
    repository = github_actions.normalize_repository(repository)
    hostname = github_actions.normalize_hostname(hostname)
    number = github_actions.positive_integer(pr_number, field="pr-number")
    expected_sha = validate_sha(expected_head_sha)
    if repository.casefold() not in {
        item.casefold() for item in config["allowed_repositories"]
    }:
        raise GitHubPullRequestError(f"repository is not allowlisted: {repository}")
    if hostname not in config["allowed_hosts"]:
        raise GitHubPullRequestError(f"hostname is not allowlisted: {hostname}")
    if review_policy not in REVIEW_POLICIES:
        raise GitHubPullRequestError("review-policy must be ignore or approved")
    if wake_policy not in WAKE_POLICIES:
        raise GitHubPullRequestError(
            "wake-policy must be one of: " + ", ".join(sorted(WAKE_POLICIES))
        )
    interval = bounded_setting(
        interval_seconds,
        field="interval-seconds",
        maximum=MAX_INTERVAL_SECONDS,
    )
    timeout = bounded_setting(
        timeout_seconds,
        field="timeout-seconds",
        maximum=MAX_TIMEOUT_SECONDS,
    )
    executable = (gh_command or config["gh_command"]).strip()
    if not executable or any(char in executable for char in ("\n", "\r", "\x00")):
        raise GitHubPullRequestError("gh-command must be one executable name or path")
    if len(executable) > github_actions.MAX_COMMAND_LENGTH:
        raise GitHubPullRequestError("gh-command is too long")
    normalized_retry_reason = None
    if retry_of is not None:
        normalized_retry_reason = github_actions.bounded_reason(
            retry_reason or "", field="retry reason"
        )
    identity = {
        "hostname": hostname,
        "repository": repository,
        "pr_number": number,
    }
    dispatch_identity = {
        **identity,
        "expected_head_sha": expected_sha,
        "review_policy": review_policy,
        "interval_seconds": interval,
        "timeout_seconds": timeout,
        "wake_policy": wake_policy,
        "gh_command": executable,
        "retry_of": retry_of,
        "retry_reason": normalized_retry_reason,
    }
    resolved_id = github_actions.validate_monitor_id(
        monitor_id
        or default_monitor_id(
            hostname=hostname,
            repository=repository,
            pr_number=number,
            expected_head_sha=expected_sha,
            review_policy=review_policy,
        )
    )
    directory = monitor_dir_for(project, resolved_id, state_dir=state_dir)
    descriptor_path = directory / "monitor.json"
    descriptor: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": MONITOR_KIND,
        "monitor_id": resolved_id,
        "source_kind": SOURCE_KIND,
        "status": "starting",
        **identity,
        "expected_head_sha": expected_sha,
        "review_policy": review_policy,
        "interval_seconds": interval,
        "timeout_seconds": timeout,
        "wake_policy": wake_policy,
        "gh_command": executable,
        "monitor_dir": str(directory),
        "supervisor_log": str(directory / "supervisor.log"),
        "created_at": core.utc_now(),
    }
    if retry_of is not None:
        descriptor["retry_of"] = github_actions.validate_monitor_id(retry_of)
        descriptor["retry_reason"] = normalized_retry_reason
    bound = binding.load_binding(project, state_dir=state_dir)
    if bound is not None:
        descriptor["wake_target"] = binding.wake_target_from_binding(bound)
    with admission_lock(project, state_dir=state_dir):
        for path in sorted(
            monitor_root(project, state_dir=state_dir).glob("*/monitor.json")
        ):
            existing = core.load_object(path)
            existing_identity = {
                key: existing.get(key)
                for key in ("hostname", "repository", "pr_number")
            }
            if (
                existing_identity["hostname"] != identity["hostname"]
                or not github_actions.same_repository(
                    existing_identity["repository"], identity["repository"]
                )
                or existing_identity["pr_number"] != identity["pr_number"]
            ):
                continue
            existing_dispatch = {
                key: existing.get(key) for key in dispatch_identity
            }
            if path == descriptor_path:
                comparable_existing = {
                    **existing_dispatch,
                    "repository": str(existing_dispatch["repository"]).casefold(),
                }
                comparable_dispatch = {
                    **dispatch_identity,
                    "repository": str(dispatch_identity["repository"]).casefold(),
                }
                if comparable_existing != comparable_dispatch:
                    raise GitHubPullRequestError(
                        "monitor already exists with different dispatch options: "
                        f"{resolved_id}"
                    )
                return {**existing, "descriptor_path": str(path), "idempotent": True}
            if existing.get("status") not in TERMINAL_STATUSES:
                raise GitHubPullRequestError(
                    "an active monitor already owns this repository/PR: "
                    f"{existing.get('monitor_id')}"
                )
        try:
            verification.claim_check_owner(
                project,
                operation_id=resolved_id,
                operation_type="github_pull_request",
                state_dir=state_dir,
            )
        except verification.VerificationError as error:
            raise GitHubPullRequestError(str(error)) from error
        directory.mkdir(parents=True, exist_ok=True)
        if not core.claim_json(descriptor_path, descriptor):
            raise GitHubPullRequestError(f"monitor already exists: {resolved_id}")
    try:
        with Path(descriptor["supervisor_log"]).open("ab") as log:
            command = supervisor_command(
                project,
                monitor_id=resolved_id,
                state_dir=state_dir,
            )
            process = popen_factory(
                command,
                cwd=str(project),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as error:
        message = github_actions.redact_text(str(error))[:MAX_REASON_LENGTH]
        finalize_monitor(
            project,
            descriptor,
            status="failed",
            failure_kind="supervisor_launch_failed",
            snapshot=None,
            command_evidence=None,
            state_dir=state_dir,
            started_at=str(descriptor["created_at"]),
            duration_seconds=0.0,
            error=message,
        )
        raise GitHubPullRequestError(
            f"could not launch PR monitor: {message}"
        ) from error
    launch = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "GITHUB_PR_MONITOR_SUPERVISOR_LAUNCH",
        "monitor_id": resolved_id,
        "supervisor_pid": int(process.pid),
        "launched_at": core.utc_now(),
    }
    identity_token = worker_lease.process_identity(int(process.pid))
    if identity_token is not None:
        launch["supervisor_identity"] = identity_token
    core.atomic_json(launch_path(directory), launch)
    threading.Thread(target=reap_process, args=(process,), daemon=True).start()
    return {
        **descriptor,
        "supervisor_pid": int(process.pid),
        "supervisor_launch_recorded": True,
        "descriptor_path": str(descriptor_path),
        "idempotent": False,
    }


def check_name(item: dict[str, Any]) -> str:
    value = item.get("name") or item.get("context") or "unnamed"
    return github_actions.redact_text(str(value))[:MAX_CHECK_NAME]


def normalize_snapshot(view: dict[str, Any]) -> dict[str, Any]:
    checks = view.get("statusCheckRollup")
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise GitHubPullRequestError("statusCheckRollup is invalid or too large")
    counts = {"passed": 0, "pending": 0, "failed": 0}
    failed_names: list[str] = []
    for item in checks:
        if not isinstance(item, dict):
            raise GitHubPullRequestError("statusCheckRollup item is invalid")
        typename = item.get("__typename")
        if typename == "CheckRun":
            status = str(item.get("status") or "").upper()
            conclusion = str(item.get("conclusion") or "").upper()
            if status != "COMPLETED" or not conclusion:
                category = "pending"
            elif conclusion in CHECK_FAILURES:
                category = "failed"
            elif conclusion in CHECK_SUCCESSES:
                category = "passed"
            else:
                raise GitHubPullRequestError(
                    f"unsupported check conclusion: {conclusion}"
                )
        elif typename == "StatusContext":
            state = str(item.get("state") or "").upper()
            if state in STATUS_FAILURES:
                category = "failed"
            elif state in STATUS_SUCCESSES:
                category = "passed"
            elif state in {"EXPECTED", "PENDING"}:
                category = "pending"
            else:
                raise GitHubPullRequestError(f"unsupported status state: {state}")
        else:
            raise GitHubPullRequestError(f"unsupported status check type: {typename}")
        counts[category] += 1
        if category == "failed" and len(failed_names) < MAX_FAILED_NAMES:
            failed_names.append(check_name(item))
    number = view.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise GitHubPullRequestError("PR number is invalid")
    is_draft = view.get("isDraft")
    if not isinstance(is_draft, bool):
        raise GitHubPullRequestError("isDraft is invalid")
    head_sha = str(view.get("headRefOid") or "").lower()
    validate_sha(head_sha)
    state = str(view.get("state") or "").upper()
    if state not in {"OPEN", "CLOSED", "MERGED"}:
        raise GitHubPullRequestError(f"unsupported PR state: {state}")
    review_decision = str(view.get("reviewDecision") or "").upper()
    if review_decision not in {
        "",
        "APPROVED",
        "CHANGES_REQUESTED",
        "REVIEW_REQUIRED",
    }:
        raise GitHubPullRequestError(
            f"unsupported review decision: {review_decision}"
        )
    mergeable = str(view.get("mergeable") or "").upper()
    if mergeable not in {"MERGEABLE", "CONFLICTING", "UNKNOWN"}:
        raise GitHubPullRequestError(f"unsupported mergeable state: {mergeable}")
    url = view.get("url")
    if (
        not isinstance(url, str)
        or not url.startswith("https://")
        or len(url) > 2048
    ):
        raise GitHubPullRequestError("PR URL is invalid")
    return {
        "number": number,
        "state": state,
        "is_draft": is_draft,
        "head_sha": head_sha,
        "review_decision": review_decision,
        "mergeable": mergeable,
        "url": url,
        "check_count": len(checks),
        "check_counts": counts,
        "failed_check_names": failed_names,
    }


def classify_cli_error(stderr: str) -> str:
    lowered = stderr.casefold()
    if any(marker in lowered for marker in PR_NOT_FOUND_MARKERS):
        return "pull_request_not_found"
    return github_actions.classify_cli_error(stderr)


def run_view(
    descriptor: dict[str, Any],
    *,
    runner=None,
    timeout_seconds: float = github_actions.VIEW_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    command = [
        str(descriptor["gh_command"]),
        "pr",
        "view",
        str(descriptor["pr_number"]),
        "--repo",
        github_actions.repo_argument(
            str(descriptor["hostname"]), str(descriptor["repository"])
        ),
        "--json",
        VIEW_FIELDS,
    ]
    started = time.monotonic()
    if runner is None:
        try:
            completed = github_actions.run_bounded_command(
                command, timeout_seconds=timeout_seconds
            )
        except OSError as error:
            data = str(error).encode("utf-8", errors="replace")
            return {
                "ok": False,
                "failure_kind": "gh_command_failed",
                "exit_code": None,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": github_actions.command_capture(b"", tail=False),
                "stderr": github_actions.command_capture(data),
            }
        stdout = completed["stdout_bytes"]
        stderr = completed["stderr_bytes"]
        evidence = {
            "exit_code": completed["returncode"],
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": completed["stdout"],
            "stderr": completed["stderr"],
        }
        if completed["timed_out"]:
            return {**evidence, "ok": False, "failure_kind": "view_timeout"}
        if completed["stdout"]["size_bytes"] > github_actions.MAX_VIEW_BYTES:
            return {**evidence, "ok": False, "failure_kind": "view_output_too_large"}
    else:
        try:
            result = runner(
                command,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            data = str(error).encode("utf-8", errors="replace")
            return {
                "ok": False,
                "failure_kind": (
                    "view_timeout"
                    if isinstance(error, subprocess.TimeoutExpired)
                    else "gh_command_failed"
                ),
                "exit_code": None,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": github_actions.command_capture(b"", tail=False),
                "stderr": github_actions.command_capture(data),
            }
        stdout = result.stdout or b""
        stderr = result.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        evidence = {
            "exit_code": int(result.returncode),
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": github_actions.command_capture(stdout, tail=False),
            "stderr": github_actions.command_capture(stderr),
        }
        if len(stdout) > github_actions.MAX_VIEW_BYTES:
            return {**evidence, "ok": False, "failure_kind": "view_output_too_large"}
    if evidence["exit_code"] != 0:
        return {
            **evidence,
            "ok": False,
            "failure_kind": classify_cli_error(github_actions.bounded_tail(stderr)),
        }
    try:
        value = json.loads(stdout.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("PR view must be an object")
        snapshot = normalize_snapshot(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return {
            **evidence,
            "ok": False,
            "failure_kind": "invalid_view_json",
            "error": github_actions.redact_text(str(error))[:MAX_REASON_LENGTH],
        }
    except GitHubPullRequestError as error:
        return {
            **evidence,
            "ok": False,
            "failure_kind": "invalid_view_contract",
            "error": github_actions.redact_text(str(error))[:MAX_REASON_LENGTH],
        }
    return {**evidence, "ok": True, "snapshot": snapshot}


def evaluate_snapshot(
    descriptor: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[str, str | None]:
    if not github_actions.repository_url_matches(
        snapshot.get("url"),
        hostname=str(descriptor.get("hostname", "")),
        repository=str(descriptor.get("repository", "")),
    ):
        return "ambiguous", "repository_url_mismatch"
    if snapshot.get("number") != descriptor.get("pr_number"):
        return "ambiguous", "pr_number_mismatch"
    if snapshot.get("head_sha") != descriptor.get("expected_head_sha"):
        return "head_changed", "head_sha_mismatch"
    state = snapshot.get("state")
    if state == "MERGED":
        return "merged", None
    if state == "CLOSED":
        return "closed", "pull_request_closed"
    if state != "OPEN":
        return "ambiguous", "unsupported_pr_state"
    if snapshot.get("mergeable") == "CONFLICTING":
        return "conflicting", "merge_conflict"
    if snapshot.get("check_counts", {}).get("failed", 0):
        return "failed_checks", "check_failed"
    if snapshot.get("is_draft") is True:
        return "waiting", "draft_pull_request"
    if snapshot.get("mergeable") == "UNKNOWN":
        return "waiting", "mergeability_pending"
    if descriptor.get("review_policy") == "approved":
        decision = snapshot.get("review_decision")
        if decision == "CHANGES_REQUESTED":
            return "changes_requested", "review_changes_requested"
        if decision != "APPROVED":
            return "waiting", "approval_pending"
    if snapshot.get("check_counts", {}).get("pending", 0):
        return "waiting", "checks_pending"
    return "ready", None


def poll_until_terminal(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
    runner=None,
    sleep=time.sleep,
) -> dict[str, Any]:
    started = time.monotonic()
    sample_count = 0
    first: dict[str, Any] | None = None
    last_snapshot: dict[str, Any] | None = None
    last_reason = "readiness_timeout"
    while True:
        directory = Path(str(descriptor["monitor_dir"]))
        if cancel_path(directory).is_file():
            return {
                "status": "cancelled",
                "failure_kind": "operator_cancelled",
                "snapshot": None,
                "command_evidence": first,
                "sample_count": sample_count,
            }
        elapsed = time.monotonic() - started
        remaining = float(descriptor["timeout_seconds"]) - elapsed
        if remaining <= 0:
            return {
                "status": "timed_out",
                "failure_kind": last_reason,
                "snapshot": last_snapshot,
                "command_evidence": first,
                "sample_count": sample_count,
            }
        observed = run_view(
            descriptor,
            runner=runner,
            timeout_seconds=min(github_actions.VIEW_TIMEOUT_SECONDS, remaining),
        )
        sample_count += 1
        if first is None:
            first = observed
        if not observed.get("ok"):
            return {
                "status": "unavailable",
                "failure_kind": observed.get("failure_kind"),
                "snapshot": None,
                "command_evidence": observed,
                "sample_count": sample_count,
                "error": observed.get("error"),
            }
        snapshot = observed["snapshot"]
        last_snapshot = snapshot
        status, reason = evaluate_snapshot(descriptor, snapshot)
        last_reason = reason or "readiness_timeout"
        if status != "waiting":
            return {
                "status": status,
                "failure_kind": reason,
                "snapshot": snapshot,
                "command_evidence": observed,
                "sample_count": sample_count,
            }
        elapsed = time.monotonic() - started
        if elapsed >= float(descriptor["timeout_seconds"]):
            return {
                "status": "timed_out",
                "failure_kind": reason or "readiness_timeout",
                "snapshot": snapshot,
                "command_evidence": observed,
                "sample_count": sample_count,
            }
        descriptor["last_alive_at"] = core.utc_now()
        descriptor["last_state"] = reason
        descriptor["sample_count"] = sample_count
        core.atomic_json(directory / "monitor.json", descriptor)
        wait_seconds = min(
            float(descriptor["interval_seconds"]),
            max(float(descriptor["timeout_seconds"]) - elapsed, 0.01),
        )
        while wait_seconds > 0:
            if cancel_path(directory).is_file():
                return {
                    "status": "cancelled",
                    "failure_kind": "operator_cancelled",
                    "snapshot": snapshot,
                    "command_evidence": observed,
                    "sample_count": sample_count,
                }
            step = min(wait_seconds, 1.0)
            sleep(step)
            wait_seconds -= step


def verification_status(status: str) -> str:
    if status in SUCCESS_STATUSES:
        return "passed"
    if status == "failed_checks":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "unknown"


def event_status(status: str) -> str:
    if status in SUCCESS_STATUSES:
        return "completed"
    if status == "failed_checks" or status == "failed":
        return "failed"
    if status in {"cancelled", "closed"}:
        return "cancelled"
    if status == "timed_out":
        return "timed_out"
    if status == "unavailable":
        return "unavailable"
    if status == "ambiguous":
        return "ambiguous"
    return "action_required"


def should_wake(policy: str, status: str) -> bool:
    if policy == "always":
        return True
    if policy == "on-failure":
        return status not in SUCCESS_STATUSES
    return status in ACTION_STATUSES


def finalize_monitor(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    status: str,
    failure_kind: str | None,
    snapshot: dict[str, Any] | None,
    command_evidence: dict[str, Any] | None,
    state_dir: str,
    started_at: str,
    duration_seconds: float,
    sample_count: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    monitor_id = str(descriptor["monitor_id"])
    directory = Path(str(descriptor["monitor_dir"]))
    try:
        verification.claim_check_owner(
            project,
            operation_id=monitor_id,
            operation_type="github_pull_request",
            state_dir=state_dir,
        )
    except verification.VerificationError as error:
        raise GitHubPullRequestError(str(error)) from error
    check_dir = verification.checks_root(project, state_dir=state_dir) / monitor_id
    check_dir.mkdir(parents=True, exist_ok=True)
    finished_at = core.utc_now()
    summary = (
        f"GitHub PR {descriptor['repository']}#{descriptor['pr_number']}: "
        f"status={status} checks={(snapshot or {}).get('check_counts', {})} "
        f"review={(snapshot or {}).get('review_decision') or 'unavailable'}"
    )[:1000]
    summary_path = check_dir / "summary.txt"
    summary_path.write_text(summary + "\n", encoding="utf-8")
    result_path = check_dir / "verification-result.json"
    result = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": verification.VERIFICATION_RESULT_KIND,
        "check_id": monitor_id,
        "suite": "github-pr-readiness",
        "status": verification_status(status),
        "exit_code": 0 if status in SUCCESS_STATUSES else 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 3),
        "commands": [],
        "result_path": str(result_path),
        "summary_path": str(summary_path),
        "log_path": str(directory / "supervisor.log"),
        "github_pull_request": {
            "hostname": descriptor["hostname"],
            "repository": descriptor["repository"],
            "pr_number": descriptor["pr_number"],
            "expected_head_sha": descriptor["expected_head_sha"],
            "review_policy": descriptor["review_policy"],
            "monitor_status": status,
            "failure_kind": failure_kind,
            "snapshot": snapshot,
        },
    }
    core.atomic_json(result_path, result)
    evidence_path = directory / "evidence.json"
    evidence: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "monitor_id": monitor_id,
        "source_kind": SOURCE_KIND,
        "hostname": descriptor["hostname"],
        "repository": descriptor["repository"],
        "pr_number": descriptor["pr_number"],
        "expected_head_sha": descriptor["expected_head_sha"],
        "review_policy": descriptor["review_policy"],
        "monitor_status": status,
        "failure_kind": failure_kind,
        "snapshot": snapshot,
        "command_evidence": command_evidence,
        "sample_count": sample_count,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 3),
        "summary": summary,
        "result_path": str(result_path),
        "summary_path": str(summary_path),
    }
    if error:
        evidence["error"] = github_actions.redact_text(error)[:MAX_REASON_LENGTH]
    if isinstance(descriptor.get("wake_target"), dict):
        evidence["wake_target"] = descriptor["wake_target"]
    core.atomic_json(evidence_path, evidence)
    emitted = core.write_followup_event(
        project,
        operation_id=monitor_id,
        source_kind=SOURCE_KIND,
        terminal_status=event_status(status),
        result_path=result_path,
        evidence_path=evidence_path,
        state_dir=state_dir,
        wake_target=(
            descriptor.get("wake_target")
            if isinstance(descriptor.get("wake_target"), dict)
            else None
        ),
        emit_signal=should_wake(str(descriptor["wake_policy"]), status),
    )
    final = {
        **descriptor,
        "status": status,
        "failure_kind": failure_kind,
        "snapshot": snapshot,
        "sample_count": sample_count,
        "started_at": started_at,
        "finished_at": finished_at,
        "last_alive_at": finished_at,
        "result_path": str(result_path),
        "summary_path": str(summary_path),
        "evidence_path": str(evidence_path),
        "event_path": emitted["event_path"],
        "signal_path": emitted["signal_path"],
        "signal_emitted": emitted["signal_emitted"],
    }
    core.atomic_json(directory / "monitor.json", final)
    return final


def supervise_monitor(
    project_root: Path,
    *,
    monitor_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    runner=None,
    sleep=time.sleep,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    directory = monitor_dir_for(project, monitor_id, state_dir=state_dir)
    path = directory / "monitor.json"
    identity = worker_lease.process_identity(os.getpid())
    if identity is None:
        raise GitHubPullRequestError("cannot record PR monitor process identity")
    with admission_lock(project, state_dir=state_dir):
        descriptor = core.load_object(path)
        if (
            not core.is_supported_schema_version(descriptor.get("schema_version"))
            or descriptor.get("kind") != MONITOR_KIND
        ):
            raise GitHubPullRequestError("monitor descriptor contract is invalid")
        if descriptor.get("status") in TERMINAL_STATUSES:
            return descriptor
        if descriptor.get("status") == "running":
            existing_state = process_state(directory, descriptor)
            if existing_state["state"] == "gone":
                raise GitHubPullRequestError(
                    "monitor supervisor is gone; run pr reap before retrying"
                )
            raise GitHubPullRequestError("monitor is already owned by a supervisor")
        started_at = core.utc_now()
        descriptor.update(
            status="running",
            supervisor_pid=os.getpid(),
            supervisor_identity=identity,
            started_at=started_at,
            last_alive_at=started_at,
        )
        core.atomic_json(path, descriptor)
    started = time.monotonic()
    try:
        outcome = poll_until_terminal(
            project,
            descriptor,
            state_dir=state_dir,
            runner=runner,
            sleep=sleep,
        )
    except Exception as error:
        outcome = {
            "status": "failed",
            "failure_kind": "monitor_process_failure",
            "snapshot": None,
            "command_evidence": None,
            "sample_count": 0,
            "error": str(error),
        }
    return finalize_monitor(
        project,
        descriptor,
        status=str(outcome["status"]),
        failure_kind=outcome.get("failure_kind"),
        snapshot=outcome.get("snapshot"),
        command_evidence=outcome.get("command_evidence"),
        state_dir=state_dir,
        started_at=started_at,
        duration_seconds=time.monotonic() - started,
        sample_count=int(outcome.get("sample_count", 0)),
        error=outcome.get("error"),
    )


def cancel_monitor(
    project_root: Path,
    *,
    monitor_id: str,
    reason: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    normalized = github_actions.bounded_reason(reason, field="cancel reason")
    directory = monitor_dir_for(project_root, monitor_id, state_dir=state_dir)
    descriptor = core.load_object(directory / "monitor.json")
    if descriptor.get("status") in TERMINAL_STATUSES:
        return {**descriptor, "cancel_requested": False, "idempotent": True}
    request = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "GITHUB_PR_MONITOR_CANCEL_REQUEST",
        "monitor_id": monitor_id,
        "reason": normalized,
        "requested_at": core.utc_now(),
    }
    path = cancel_path(directory)
    if path.exists():
        return {**core.load_object(path), "path": str(path), "idempotent": True}
    core.atomic_json(path, request)
    return {**request, "path": str(path), "idempotent": False}


def process_state(directory: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    identity = descriptor.get("supervisor_identity")
    pid = descriptor.get("supervisor_pid")
    if not isinstance(identity, dict) and launch_path(directory).is_file():
        with contextlib.suppress(OSError, core.OrchestratorError):
            launch = core.load_object(launch_path(directory))
            identity = launch.get("supervisor_identity")
            pid = launch.get("supervisor_pid")
    return (
        worker_lease.identity_state(identity)
        if isinstance(identity, dict)
        else worker_lease.pid_state(pid)
    )


def recover_completed_monitor(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
) -> dict[str, Any] | None:
    """Recover a descriptor when terminal result and evidence already exist."""

    project = project_root.expanduser().resolve()
    monitor_id = str(descriptor["monitor_id"])
    directory = Path(str(descriptor["monitor_dir"]))
    check_dir = verification.checks_root(project, state_dir=state_dir) / monitor_id
    result_path = check_dir / "verification-result.json"
    summary_path = check_dir / "summary.txt"
    evidence_path = directory / "evidence.json"
    if not (
        result_path.is_file()
        and summary_path.is_file()
        and evidence_path.is_file()
    ):
        return None
    try:
        result = core.load_object(result_path)
        evidence = core.load_object(evidence_path)
    except (OSError, core.OrchestratorError):
        return None
    status = evidence.get("monitor_status")
    if (
        result.get("kind") != verification.VERIFICATION_RESULT_KIND
        or result.get("check_id") != monitor_id
        or evidence.get("kind") != EVIDENCE_KIND
        or evidence.get("monitor_id") != monitor_id
        or status not in TERMINAL_STATUSES
    ):
        return None
    emitted = core.write_followup_event(
        project,
        operation_id=monitor_id,
        source_kind=SOURCE_KIND,
        terminal_status=event_status(str(status)),
        result_path=result_path,
        evidence_path=evidence_path,
        state_dir=state_dir,
        wake_target=(
            descriptor.get("wake_target")
            if isinstance(descriptor.get("wake_target"), dict)
            else None
        ),
        emit_signal=should_wake(str(descriptor["wake_policy"]), str(status)),
    )
    descriptor.update(
        status=status,
        failure_kind=evidence.get("failure_kind"),
        snapshot=evidence.get("snapshot"),
        sample_count=evidence.get("sample_count", 0),
        started_at=evidence.get("started_at"),
        finished_at=evidence.get("finished_at"),
        last_alive_at=evidence.get("finished_at"),
        result_path=str(result_path),
        summary_path=str(summary_path),
        evidence_path=str(evidence_path),
        event_path=emitted["event_path"],
        signal_path=emitted["signal_path"],
        signal_emitted=emitted["signal_emitted"],
        recovered_at=core.utc_now(),
    )
    core.atomic_json(directory / "monitor.json", descriptor)
    return descriptor


def timestamp_age(value: object) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        return 0.0
    return max((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds(), 0.0)


def reap_monitors(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    platform_runtime.require_detached_lifecycle("pr reap")
    project = project_root.expanduser().resolve()
    outcomes = []
    for path in sorted(
        monitor_root(project, state_dir=state_dir).glob("*/monitor.json")
    ):
        with admission_lock(project, state_dir=state_dir):
            try:
                descriptor = core.load_object(path)
            except (OSError, core.OrchestratorError) as error:
                outcomes.append(
                    {
                        "monitor_id": path.parent.name,
                        "status": "invalid",
                        "reason": github_actions.redact_text(str(error))[
                            :MAX_REASON_LENGTH
                        ],
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
                        or path.parent.name,
                        "status": "invalid",
                        "reason": "descriptor contract is invalid",
                    }
                )
                continue
            if descriptor.get("status") in TERMINAL_STATUSES:
                continue
            state = process_state(path.parent, descriptor)
            if state["state"] == "alive":
                outcomes.append(
                    {
                        "monitor_id": descriptor.get("monitor_id"),
                        "status": "supervisor_alive",
                    }
                )
                continue
            if state["state"] == "unknown":
                outcomes.append(
                    {
                        "monitor_id": descriptor.get("monitor_id"),
                        "status": "unsafe_missing_identity",
                    }
                )
                continue
            recovered = recover_completed_monitor(
                project,
                descriptor,
                state_dir=state_dir,
            )
            if recovered is not None:
                outcomes.append(
                    {
                        "monitor_id": descriptor.get("monitor_id"),
                        "status": "recovered",
                        "terminal_status": recovered.get("status"),
                        "event_path": recovered.get("event_path"),
                    }
                )
                continue
            failure_kind = (
                "supervisor_never_claimed"
                if descriptor.get("status") == "starting"
                else "supervisor_not_alive"
            )
            final = finalize_monitor(
                project,
                descriptor,
                status="failed",
                failure_kind=failure_kind,
                snapshot=None,
                command_evidence=None,
                state_dir=state_dir,
                started_at=str(
                    descriptor.get("started_at") or descriptor["created_at"]
                ),
                duration_seconds=timestamp_age(
                    descriptor.get("started_at") or descriptor.get("created_at")
                ),
                error="detached PR monitor supervisor exited before finalization",
            )
            outcomes.append(
                {
                    "monitor_id": descriptor.get("monitor_id"),
                    "status": "reaped",
                    "event_path": final.get("event_path"),
                }
            )
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "GITHUB_PR_MONITOR_REAP_REPORT",
        "project_root": str(project),
        "reaped_count": sum(item["status"] == "reaped" for item in outcomes),
        "recovered_count": sum(
            item["status"] == "recovered" for item in outcomes
        ),
        "outcomes": outcomes,
    }


def retry_monitor(
    project_root: Path,
    *,
    monitor_id: str,
    reason: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    normalized = github_actions.bounded_reason(reason, field="retry reason")
    project = project_root.expanduser().resolve()
    original = core.load_object(
        monitor_dir_for(project, monitor_id, state_dir=state_dir) / "monitor.json"
    )
    if original.get("status") not in TERMINAL_STATUSES - SUCCESS_STATUSES:
        raise GitHubPullRequestError(
            "only an unsuccessful terminal monitor can be retried"
        )
    index = 1
    while monitor_dir_for(
        project, f"{monitor_id}-r{index}", state_dir=state_dir
    ).exists():
        index += 1
    return start_monitor(
        project,
        repository=str(original["repository"]),
        pr_number=int(original["pr_number"]),
        expected_head_sha=str(original["expected_head_sha"]),
        state_dir=state_dir,
        hostname=str(original["hostname"]),
        review_policy=str(original["review_policy"]),
        interval_seconds=float(original["interval_seconds"]),
        timeout_seconds=float(original["timeout_seconds"]),
        wake_policy=str(original["wake_policy"]),
        monitor_id=f"{monitor_id}-r{index}",
        retry_of=monitor_id,
        retry_reason=normalized,
        popen_factory=popen_factory,
    )


def monitor_status(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    monitor_id: str | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    paths = (
        [monitor_dir_for(project, monitor_id, state_dir=state_dir) / "monitor.json"]
        if monitor_id is not None
        else sorted(monitor_root(project, state_dir=state_dir).glob("*/monitor.json"))
    )
    monitors = []
    for path in paths:
        if not path.is_file():
            raise GitHubPullRequestError(f"unknown monitor: {monitor_id}")
        try:
            descriptor = core.load_object(path)
        except (OSError, core.OrchestratorError) as error:
            if monitor_id is not None:
                raise GitHubPullRequestError(
                    f"monitor descriptor is unreadable: {path}: {error}"
                ) from error
            monitors.append(
                {
                    "monitor_id": path.parent.name,
                    "status": "invalid",
                    "failure_kind": "descriptor_unreadable",
                    "error": github_actions.redact_text(str(error))[
                        :MAX_REASON_LENGTH
                    ],
                    "suggested_action": (
                        "Inspect the descriptor without deleting durable artifacts."
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
                        "delete durable artifacts blindly."
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
                "pr_number",
                "expected_head_sha",
                "review_policy",
                "failure_kind",
                "last_state",
                "sample_count",
                "created_at",
                "started_at",
                "finished_at",
                "result_path",
                "summary_path",
                "evidence_path",
                "event_path",
                "signal_path",
                "signal_emitted",
                "wake_policy",
            )
        }
        if descriptor.get("status") in {"starting", "running"}:
            state = process_state(path.parent, descriptor)
            summary["supervisor_state"] = state["state"]
            if state["state"] == "gone":
                summary["status"] = "crashed"
                summary["failure_kind"] = "supervisor_not_alive"
                summary["suggested_action"] = (
                    "Run pr reap, then retry explicitly if appropriate."
                )
            elif state["state"] == "unknown" and timestamp_age(
                descriptor.get("created_at")
            ) > STARTING_GRACE_SECONDS:
                summary["status"] = "stalled"
                summary["failure_kind"] = "supervisor_identity_unavailable"
        monitors.append(summary)
    counts: dict[str, int] = {}
    for item in monitors:
        key = str(item.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": STATUS_KIND,
        "project_root": str(project),
        "monitor_count": len(monitors),
        "status_counts": counts,
        "monitors": monitors,
        "checked_at": core.utc_now(),
    }
