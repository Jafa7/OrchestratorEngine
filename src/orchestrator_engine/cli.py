"""Command-line interface for OrchestratorEngine."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    __version__,
    adoption,
    artifact_resolution,
    binding,
    claude_stream,
    codex_app,
    conformance,
    core,
    diagnostics,
    github_actions,
    github_pull_requests,
    host_capabilities,
    local_checks,
    operation_wait,
    platform_runtime,
    schemas,
    status,
    task_diagnostics,
    task_resolution,
    upgrade,
    verification,
    watcher,
    worker_diagnostics,
    worker_policy,
    workers,
    workstreams,
)


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OrchestratorEngine")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        action="append",
        default=None,
        help="Project root. Can be passed multiple times for watcher commands.",
    )
    parser.add_argument("--state-dir", default=core.DEFAULT_STATE_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)

    emit = subparsers.add_parser(
        "emit",
        help="Write a terminal event and matching inbox signal.",
    )
    emit.add_argument("--task-id", required=True)
    emit.add_argument(
        "--terminal-status",
        choices=sorted(core.TERMINAL_STATUSES),
        required=True,
    )
    emit.add_argument("--result", type=Path, required=True)
    emit.add_argument("--evidence", type=Path, required=True)
    emit.add_argument("--event-id")

    subparsers.add_parser("inbox", help="List pending inbox signals.")
    subparsers.add_parser(
        "host-capabilities",
        help="Print the read-only host delivery capability report.",
    )
    subparsers.add_parser(
        "runtime-capabilities",
        help="Print portable-core and detached-runtime platform support.",
    )
    codex_parser = subparsers.add_parser(
        "codex",
        help="Run explicit diagnostics at the Codex adapter boundary.",
    )
    codex_subparsers = codex_parser.add_subparsers(
        dest="codex_command_name", required=True
    )
    codex_diagnose = codex_subparsers.add_parser(
        "diagnose",
        help="Run bounded `codex doctor --json` and return a redacted summary.",
    )
    codex_diagnose.add_argument(
        "--codex-command",
        default=None,
        help=(
            "Override the Codex launcher. By default use the current project's "
            "Codex binding, then PATH."
        ),
    )
    codex_diagnose.add_argument(
        "--timeout-seconds",
        type=float,
        default=codex_app.CODEX_DIAGNOSTIC_DEFAULT_TIMEOUT_SECONDS,
        help="Bounded diagnostic timeout, at most 60 seconds (default: 10).",
    )
    conformance_parser = subparsers.add_parser(
        "conformance",
        help="Run provider-free clean-fixture conformance checks.",
    )
    conformance_subparsers = conformance_parser.add_subparsers(
        dest="conformance_command", required=True
    )
    conformance_run = conformance_subparsers.add_parser(
        "run",
        help="Verify durable orchestration in a new isolated fixture.",
    )
    conformance_run.add_argument(
        "--mode",
        choices=sorted(conformance.CONFORMANCE_MODES),
        default="auto",
        help="Use portable core checks, full detached checks, or auto-detect.",
    )
    conformance_run.add_argument(
        "--fixture-root",
        type=Path,
        help="Create the clean fixture at this new path instead of a temp path.",
    )
    conformance_run.add_argument(
        "--keep-fixture",
        action="store_true",
        help="Keep a successful fixture for inspection; failures are always kept.",
    )
    conformance_run.add_argument(
        "--timeout-seconds",
        type=float,
        default=30,
        help="Maximum full-mode synthetic worker wait (default: 30).",
    )
    schema_parser = subparsers.add_parser(
        "schemas", help="List or print packaged durable-artifact schemas."
    )
    schema_parser.add_argument("name", nargs="?", choices=schemas.SCHEMA_NAMES)

    doctor = subparsers.add_parser(
        "doctor",
        help="Run read-only project health diagnostics.",
    )
    doctor.add_argument(
        "--host",
        choices=sorted(binding.SUPPORTED_HOSTS),
        help="Check the delivery channel for one host instead of the bound host.",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="Return a non-zero exit code for warnings as well as errors.",
    )

    artifact = subparsers.add_parser(
        "artifact",
        help="Resolve or list hash-bound durable artifact findings.",
    )
    artifact_subparsers = artifact.add_subparsers(
        dest="artifact_command", required=True
    )
    artifact_resolve = artifact_subparsers.add_parser(
        "resolve",
        help="Acknowledge malformed schema metadata without changing the artifact.",
    )
    artifact_resolve.add_argument("--path", required=True)
    artifact_resolve.add_argument("--reason", required=True)
    artifact_subparsers.add_parser(
        "resolutions",
        help="List hash-bound artifact resolution records.",
    )

    status = subparsers.add_parser(
        "status",
        help="Run a compact read-only operator status report.",
    )
    status.add_argument(
        "--host",
        choices=sorted(binding.SUPPORTED_HOSTS),
        help="Check the delivery channel for one host instead of the bound host.",
    )
    status.add_argument(
        "--since",
        dest="since_cursor",
        help="Return full bodies only for components changed since this opaque cursor.",
    )
    status.add_argument(
        "--severity",
        choices=worker_diagnostics.SEVERITIES,
        default="warning",
        help="Minimum task/check diagnostic severity to include.",
    )
    status.add_argument(
        "--stale-after-seconds",
        type=float,
        default=task_diagnostics.DEFAULT_STALE_AFTER_SECONDS,
        help="Running task heartbeat age that should be considered stale.",
    )
    status.add_argument(
        "--large-log-bytes",
        type=int,
        default=task_diagnostics.DEFAULT_LARGE_LOG_BYTES,
        help="Worker log size that should be considered too large for chat.",
    )

    operation = subparsers.add_parser(
        "operation",
        help="Inspect or wait on a bounded mix of orchestration operations.",
    )
    operation_subparsers = operation.add_subparsers(
        dest="operation_command", required=True
    )
    operation_status_parser = operation_subparsers.add_parser(
        "status",
        help="Print one bounded worker/check/CI/PR state snapshot.",
    )
    operation_status_parser.add_argument(
        "--target",
        action="append",
        required=True,
        help="KIND:ID target; KIND is worker, check, ci or pr. Repeat as needed.",
    )
    operation_status_parser.add_argument(
        "--mode",
        choices=sorted(operation_wait.WAIT_MODES),
        default="all",
    )
    operation_status_parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=task_diagnostics.DEFAULT_STALE_AFTER_SECONDS,
    )
    operation_wait_parser = operation_subparsers.add_parser(
        "wait",
        help="Block on worker/check/CI/PR state without model polling.",
    )
    operation_wait_parser.add_argument(
        "--target",
        action="append",
        required=True,
        help="KIND:ID target; KIND is worker, check, ci or pr. Repeat as needed.",
    )
    operation_wait_parser.add_argument(
        "--mode",
        choices=sorted(operation_wait.WAIT_MODES),
        default="all",
    )
    operation_wait_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=2.0,
        help="Seconds between bounded local state reads.",
    )
    operation_wait_parser.add_argument("--timeout-seconds", type=float)
    operation_wait_parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=task_diagnostics.DEFAULT_STALE_AFTER_SECONDS,
    )
    operation_wait_parser.add_argument(
        "--json",
        action="store_true",
        help="Suppress live display and print one final bounded JSON object.",
    )
    operation_wait_parser.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto"
    )
    operation_wait_parser.add_argument(
        "--bell", choices=("auto", "always", "never"), default="auto"
    )

    report = subparsers.add_parser(
        "report",
        help="Draft structured operator reports for OrchestratorEngine triage.",
    )
    report_subparsers = report.add_subparsers(dest="report_command", required=True)
    report_draft = report_subparsers.add_parser(
        "draft",
        help="Print a Markdown GitHub issue draft from the compact status report.",
    )
    report_draft.add_argument(
        "--project-name",
        help="Human-readable adopter project name for the report title.",
    )
    report_draft.add_argument(
        "--type",
        choices=("runtime-report", "integration-finding", "core-bug"),
        default="runtime-report",
        help="Report class to place in the draft title.",
    )
    report_draft.add_argument(
        "--host",
        choices=sorted(binding.SUPPORTED_HOSTS),
        help="Check the delivery channel for one host instead of the bound host.",
    )
    report_draft.add_argument(
        "--severity",
        choices=worker_diagnostics.SEVERITIES,
        default="warning",
        help="Minimum task/check diagnostic severity to include.",
    )
    report_draft.add_argument(
        "--stale-after-seconds",
        type=float,
        default=task_diagnostics.DEFAULT_STALE_AFTER_SECONDS,
        help="Running task heartbeat age that should be considered stale.",
    )
    report_draft.add_argument(
        "--large-log-bytes",
        type=int,
        default=task_diagnostics.DEFAULT_LARGE_LOG_BYTES,
        help="Worker log size that should be considered too large for chat.",
    )
    report_draft.add_argument(
        "--output",
        type=Path,
        help="Write the Markdown draft to this file instead of stdout.",
    )

    adopt = subparsers.add_parser(
        "adopt",
        help="Create the local .orchestrator layout without overwriting files.",
    )
    adopt.add_argument(
        "--host",
        choices=sorted(binding.SUPPORTED_HOSTS),
        help="Tailor next-step instructions for this host.",
    )
    adopt.add_argument("--dry-run", action="store_true")

    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Check adopter readiness after installing a new engine version.",
    )
    upgrade_subparsers = upgrade_parser.add_subparsers(
        dest="upgrade_command", required=True
    )
    upgrade_check = upgrade_subparsers.add_parser(
        "check",
        help="Run bounded read-only version, state, policy and profile checks.",
    )
    upgrade_check.add_argument(
        "--host",
        choices=sorted(binding.SUPPORTED_HOSTS),
        help="Check this host channel instead of the bound host.",
    )
    upgrade_check.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when review is required as well as when blocked.",
    )

    bind = subparsers.add_parser(
        "bind",
        help="Declare the host target for deterministic completion delivery.",
    )
    bind_group = bind.add_mutually_exclusive_group()
    bind_group.add_argument(
        "--host",
        choices=sorted(binding.SUPPORTED_HOSTS),
        help="Host kind to bind the project to.",
    )
    bind_group.add_argument(
        "--status",
        action="store_true",
        help="Show the current binding.",
    )
    bind_group.add_argument(
        "--clear",
        action="store_true",
        help="Remove the current binding.",
    )
    bind.add_argument(
        "--thread-id",
        help="Target thread id (auto-detected for --host codex when omitted).",
    )
    bind.add_argument(
        "--codex-command",
        help=(
            "Codex launcher able to reach the bound thread "
            "(auto-detected: codex.exe for Windows Desktop threads)."
        ),
    )

    worker = subparsers.add_parser(
        "worker",
        help="Manage and dispatch CLI workers.",
    )
    worker_subparsers = worker.add_subparsers(dest="worker_command", required=True)
    worker_subparsers.add_parser(
        "list",
        help="List configured workers and their enabled state.",
    )
    availability = worker_subparsers.add_parser(
        "availability", help="Run explicit bounded availability probes."
    )
    availability.add_argument("--worker")
    availability.add_argument(
        "--all", action="store_true", help="Include disabled profiles."
    )
    worker_diagnose = worker_subparsers.add_parser(
        "diagnose",
        help="Run read-only diagnostics for configured worker profiles.",
    )
    worker_diagnose.add_argument(
        "--worker",
        help="Diagnose one worker profile instead of the full registry.",
    )
    worker_diagnose.add_argument(
        "--severity",
        choices=worker_diagnostics.SEVERITIES,
        default="info",
        help="Minimum diagnostic severity to include.",
    )
    worker_diagnose.add_argument(
        "--enabled-only",
        action="store_true",
        help="Only include enabled worker profiles.",
    )
    worker_policy_parser = worker_subparsers.add_parser(
        "policy",
        help="Inspect or export bundled worker behavior policies.",
    )
    worker_policy_subparsers = worker_policy_parser.add_subparsers(
        dest="worker_policy_command", required=True
    )
    worker_policy_export = worker_policy_subparsers.add_parser(
        "export",
        help="Export one bundled policy for explicit adopter-side comparison.",
    )
    worker_policy_export.add_argument(
        "--name",
        choices=sorted(worker_policy.BUNDLED_POLICY_SPECS),
        required=True,
    )
    worker_policy_export.add_argument("--output", type=Path, required=True)
    worker_policy_export.add_argument(
        "--replace",
        action="store_true",
        help="Explicitly replace an existing destination file.",
    )
    worker_tasks = worker_subparsers.add_parser(
        "tasks",
        help="Run read-only diagnostics for existing worker task artifacts.",
    )
    worker_tasks.add_argument(
        "--task-id",
        help="Diagnose one task id instead of every task descriptor.",
    )
    worker_tasks.add_argument(
        "--worker",
        help="Only include task descriptors for this worker profile.",
    )
    worker_tasks.add_argument(
        "--status",
        help="Only include task descriptors with this status.",
    )
    worker_tasks.add_argument(
        "--severity",
        choices=worker_diagnostics.SEVERITIES,
        default="info",
        help="Minimum diagnostic severity to include.",
    )
    worker_tasks.add_argument(
        "--stale-after-seconds",
        type=float,
        default=task_diagnostics.DEFAULT_STALE_AFTER_SECONDS,
        help="Running task heartbeat age that should be considered stale.",
    )
    worker_tasks.add_argument(
        "--large-log-bytes",
        type=int,
        default=task_diagnostics.DEFAULT_LARGE_LOG_BYTES,
        help="Worker log size that should be considered too large for chat.",
    )
    worker_wait = worker_subparsers.add_parser(
        "wait",
        help="Show compact live status until worker tasks satisfy any/all mode.",
    )
    worker_wait.add_argument(
        "--task-id",
        action="append",
        required=True,
        help="Task id to wait for; repeat to wait for multiple tasks.",
    )
    worker_wait.add_argument(
        "--mode",
        choices=sorted(workers.WAIT_MODES),
        default="all",
        help="Wait for all tasks (default) or return when any task is terminal.",
    )
    worker_wait.add_argument(
        "--interval-seconds",
        type=float,
        default=2.0,
        help="Seconds between local state reads (no model calls).",
    )
    worker_wait.add_argument(
        "--timeout-seconds",
        type=float,
        help="Stop waiting after this many seconds; omitted means no timeout.",
    )
    worker_wait.add_argument(
        "--stale-after-seconds",
        type=float,
        default=task_diagnostics.DEFAULT_STALE_AFTER_SECONDS,
        help="Stop with action_required after this task heartbeat age.",
    )
    worker_wait.add_argument(
        "--json",
        action="store_true",
        help="Suppress live display and print one final bounded JSON object.",
    )
    worker_wait.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Color policy for the interactive terminal display.",
    )
    worker_wait.add_argument(
        "--bell",
        choices=("auto", "always", "never"),
        default="auto",
        help="Terminal bell policy when the wait ends.",
    )
    worker_resolve = worker_subparsers.add_parser(
        "resolve",
        help="Mark a historical worker task outcome as operator-resolved.",
    )
    worker_resolve.add_argument("--task-id", required=True)
    worker_resolve.add_argument(
        "--status",
        choices=sorted(task_resolution.RESOLUTION_STATUSES),
        required=True,
        help="Resolution status for the historical task outcome.",
    )
    worker_resolve.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for the resolution.",
    )
    worker_resolve.add_argument(
        "--superseded-by-task-id",
        help="Successful or newer task id that supersedes this task.",
    )
    worker_resolve.add_argument(
        "--diagnostic-code",
        action="append",
        default=[],
        help=(
            "A non-error diagnostic resolved for this terminal task; "
            "repeat for multiple codes."
        ),
    )
    worker_resolve.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing resolution file for this task.",
    )
    worker_subparsers.add_parser(
        "resolutions",
        help="List operator resolutions for historical worker task outcomes.",
    )
    worker_subparsers.add_parser(
        "reap",
        help="Finalize leased tasks whose supervisor is proven gone.",
    )
    worker_queue = worker_subparsers.add_parser(
        "queue",
        help="Inspect or advance the deterministic worker queue.",
    )
    worker_queue_subparsers = worker_queue.add_subparsers(
        dest="worker_queue_command", required=True
    )
    worker_queue_subparsers.add_parser(
        "tick", help="Admit queued tasks while configured slots are available."
    )
    worker_cancel = worker_subparsers.add_parser(
        "cancel", help="Request durable cancellation of a queued or running task."
    )
    worker_cancel.add_argument("--task-id", required=True)
    worker_cancel.add_argument(
        "--mode", choices=("graceful", "forced"), default="graceful"
    )
    worker_cancel.add_argument("--reason", required=True)
    worker_run = worker_subparsers.add_parser(
        "run",
        help="Dispatch a task to a worker detached and return immediately.",
    )
    worker_run.add_argument("--worker", required=True)
    worker_run.add_argument("--task-id", required=True)
    worker_run.add_argument("--prompt-file", type=Path, required=True)
    availability_group = worker_run.add_mutually_exclusive_group()
    availability_group.add_argument(
        "--preflight-availability",
        action="store_true",
        help="Compatibility alias for --availability-mode block-unavailable.",
    )
    availability_group.add_argument(
        "--availability-mode",
        choices=("off", "block-unavailable", "require-available"),
        help="Override the configured point-in-time availability preflight mode.",
    )
    worker_run.add_argument("--intent-file", type=Path)
    worker_run.add_argument(
        "--wake-policy",
        choices=("always", "on-failure", "never"),
        default="always",
        help=(
            "Create a host follow-up signal always, only on failure, or never. "
            "Use never when this turn will use worker/operation wait."
        ),
    )
    worker_run.add_argument("--allow-duplicate", action="store_true")
    worker_run.add_argument("--duplicate-reason")
    worker_retry = worker_subparsers.add_parser(
        "retry", help="Dispatch a bounded retry with durable lineage."
    )
    worker_retry.add_argument("--task-id", required=True)
    worker_retry.add_argument("--new-task-id")
    worker_retry.add_argument("--max-attempts", type=int, default=3)
    worker_retry.add_argument("--delay-seconds", type=float, default=0.0)
    worker_retry.add_argument("--reason", required=True)
    worker_supervise = worker_subparsers.add_parser(
        "supervise",
        help="Internal: run a worker to completion and emit its terminal event.",
    )
    worker_supervise.add_argument("--worker", required=True)
    worker_supervise.add_argument("--task-id", required=True)
    worker_supervise.add_argument("--prompt-file", type=Path, required=True)

    cleanup = subparsers.add_parser(
        "cleanup",
        help=(
            "Prune old notifications, thread-wakeup receipts and rotated logs. "
            "Terminal events and inbox signals are kept as the durable audit trail."
        ),
    )
    cleanup.add_argument("--retention-days", type=int, default=30)
    cleanup.add_argument("--log-max-bytes", type=int, default=50 * 1024 * 1024)
    cleanup.add_argument("--log-keep-bytes", type=int, default=10 * 1024 * 1024)
    cleanup.add_argument("--dry-run", action="store_true")

    checks = subparsers.add_parser(
        "checks",
        help="Run read-only status diagnostics for verification check artifacts.",
    )
    checks.add_argument(
        "--check-id",
        help="Inspect one verification check id instead of every check.",
    )
    checks.add_argument(
        "--status",
        help="Only include checks with this status.",
    )
    checks.add_argument(
        "--severity",
        choices=worker_diagnostics.SEVERITIES,
        default="info",
        help="Minimum diagnostic severity to include.",
    )
    checks.add_argument(
        "--large-log-bytes",
        type=int,
        default=verification.DEFAULT_LARGE_LOG_BYTES,
        help="Verification log size that should be considered too large for chat.",
    )

    check = subparsers.add_parser(
        "check",
        help="Plan and execute deterministic local verification suites.",
    )
    check_subparsers = check.add_subparsers(dest="check_command", required=True)
    check_plan = check_subparsers.add_parser(
        "plan",
        help="Recommend foreground or detached execution from duration evidence.",
    )
    check_plan.add_argument("--suite", required=True)
    check_plan.add_argument(
        "--long-threshold-seconds",
        type=float,
        default=local_checks.DEFAULT_LONG_THRESHOLD_SECONDS,
    )
    check_run = check_subparsers.add_parser(
        "run",
        help="Run a configured suite using explicit or planned execution.",
    )
    check_run.add_argument("--check-id", required=True)
    check_run.add_argument("--suite", required=True)
    check_run.add_argument(
        "--execution",
        choices=sorted(local_checks.EXECUTION_MODES),
        default="auto",
    )
    check_run.add_argument(
        "--wake-policy",
        choices=sorted(local_checks.WAKE_POLICIES),
        default="auto",
    )
    check_run.add_argument(
        "--long-threshold-seconds",
        type=float,
        default=local_checks.DEFAULT_LONG_THRESHOLD_SECONDS,
    )
    check_status = check_subparsers.add_parser(
        "status",
        help="Read compact first-class local check runtime state.",
    )
    check_status.add_argument("--check-id")
    check_reap = check_subparsers.add_parser(
        "reap",
        help="Finalize detached checks whose recorded supervisor is gone.",
    )
    check_reap.add_argument("--check-id")
    check_supervise = check_subparsers.add_parser(
        "supervise",
        help="Internal: execute one detached local check.",
    )
    check_supervise.add_argument("--check-id", required=True)

    workstream = subparsers.add_parser(
        "workstream",
        help="Record bounded agent continuation checkpoints.",
    )
    workstream_subparsers = workstream.add_subparsers(
        dest="workstream_command", required=True
    )
    workstream_start = workstream_subparsers.add_parser(
        "start",
        help="Start a bounded workstream and snapshot the current host target.",
    )
    workstream_start.add_argument("--workstream-id", required=True)
    workstream_start.add_argument("--goal", required=True)
    workstream_start.add_argument(
        "--delay-seconds",
        type=float,
        default=workstreams.DEFAULT_DELAY_SECONDS,
    )
    workstream_start.add_argument(
        "--max-continuations",
        type=int,
        default=workstreams.DEFAULT_MAX_CONTINUATIONS,
    )
    workstream_start.add_argument(
        "--max-wall-seconds",
        type=int,
        default=workstreams.DEFAULT_MAX_WALL_SECONDS,
    )
    workstream_checkpoint = workstream_subparsers.add_parser(
        "checkpoint",
        help="Record an explicit continuation or stop decision.",
    )
    workstream_checkpoint.add_argument("--workstream-id", required=True)
    workstream_checkpoint.add_argument("--checkpoint-id", required=True)
    workstream_checkpoint.add_argument(
        "--decision", choices=sorted(workstreams.DECISIONS), required=True
    )
    workstream_checkpoint.add_argument("--summary", required=True)
    workstream_checkpoint.add_argument("--next-action")
    workstream_checkpoint.add_argument(
        "--waiting-on",
        help="External task, check or CI operation expected to wake the chat.",
    )
    workstream_checkpoint.add_argument(
        "--ready",
        action="store_true",
        help=(
            "Assert that the next action is in scope, needs no user decision "
            "and has no unfinished external prerequisite."
        ),
    )
    workstream_status = workstream_subparsers.add_parser(
        "status",
        help="Read compact durable workstream state.",
    )
    workstream_status.add_argument("--workstream-id")
    workstream_resume = workstream_subparsers.add_parser(
        "resume",
        help="Explicitly return a non-complete workstream to active state.",
    )
    workstream_resume.add_argument("--workstream-id", required=True)

    ci = subparsers.add_parser(
        "ci",
        help="Monitor external CI runs without model polling.",
    )
    ci_subparsers = ci.add_subparsers(dest="ci_command", required=True)
    ci_watch = ci_subparsers.add_parser(
        "watch",
        help="Start an exact-run or full-SHA GitHub Actions monitor.",
    )
    ci_watch.add_argument("--repo", required=True)
    ci_watch.add_argument("--run-id")
    ci_watch.add_argument("--hostname", default="github.com")
    ci_watch.add_argument("--attempt")
    ci_watch.add_argument("--expected-head-sha")
    ci_watch.add_argument(
        "--workflow-name",
        help="Exact workflow name used to disambiguate full-SHA discovery.",
    )
    ci_watch.add_argument("--gh-command")
    ci_watch.add_argument("--timeout-seconds", type=float)
    ci_watch.add_argument(
        "--wake-policy",
        choices=sorted(github_actions.WAKE_POLICIES),
        default="always",
    )
    ci_status = ci_subparsers.add_parser(
        "status",
        help="Read compact GitHub Actions monitor status.",
    )
    ci_status.add_argument("--monitor-id")
    ci_cancel = ci_subparsers.add_parser(
        "cancel",
        help="Cancel local monitoring without cancelling the GitHub run.",
    )
    ci_cancel.add_argument("--monitor-id", required=True)
    ci_cancel.add_argument("--reason", required=True)
    ci_retry = ci_subparsers.add_parser(
        "retry",
        help="Retry a terminal monitor after operator review.",
    )
    ci_retry.add_argument("--monitor-id", required=True)
    ci_retry.add_argument("--reason", required=True)
    ci_subparsers.add_parser(
        "reap",
        help="Finalize monitors whose detached supervisor is proven gone.",
    )
    ci_supervise = ci_subparsers.add_parser(
        "supervise",
        help="Internal: supervise one detached GitHub Actions monitor.",
    )
    ci_supervise.add_argument("--monitor-id", required=True)

    pr = subparsers.add_parser(
        "pr",
        help="Monitor one exact GitHub pull request readiness state.",
    )
    pr_subparsers = pr.add_subparsers(dest="pr_command", required=True)
    pr_watch = pr_subparsers.add_parser(
        "watch",
        help="Start a detached exact-PR readiness monitor.",
    )
    pr_watch.add_argument("--repo", required=True)
    pr_watch.add_argument("--pr-number", required=True)
    pr_watch.add_argument("--expected-head-sha", required=True)
    pr_watch.add_argument("--hostname", default="github.com")
    pr_watch.add_argument(
        "--review-policy",
        choices=sorted(github_pull_requests.REVIEW_POLICIES),
        default="ignore",
    )
    pr_watch.add_argument(
        "--interval-seconds",
        type=float,
        default=github_pull_requests.DEFAULT_INTERVAL_SECONDS,
    )
    pr_watch.add_argument(
        "--timeout-seconds",
        type=float,
        default=github_pull_requests.DEFAULT_TIMEOUT_SECONDS,
    )
    pr_watch.add_argument(
        "--wake-policy",
        choices=sorted(github_pull_requests.WAKE_POLICIES),
        default="always",
    )
    pr_watch.add_argument("--gh-command")
    pr_status = pr_subparsers.add_parser("status", help="Read PR monitor state.")
    pr_status.add_argument("--monitor-id")
    pr_cancel = pr_subparsers.add_parser(
        "cancel", help="Request cancellation of an active PR monitor."
    )
    pr_cancel.add_argument("--monitor-id", required=True)
    pr_cancel.add_argument("--reason", required=True)
    pr_retry = pr_subparsers.add_parser(
        "retry", help="Retry one unsuccessful terminal PR monitor."
    )
    pr_retry.add_argument("--monitor-id", required=True)
    pr_retry.add_argument("--reason", required=True)
    pr_subparsers.add_parser(
        "reap", help="Finalize PR monitors whose supervisor is proven gone."
    )
    pr_supervise = pr_subparsers.add_parser(
        "supervise", help="Internal: supervise one PR readiness monitor."
    )
    pr_supervise.add_argument("--monitor-id", required=True)

    watcher_parser = subparsers.add_parser(
        "watcher",
        help="Scan the inbox and act on unseen terminal signals.",
    )
    watcher_parser.add_argument("--state-file", type=Path)
    watcher_parser.add_argument("--codex", default="codex")
    watcher_parser.add_argument(
        "--host",
        choices=sorted(binding.SUPPORTED_HOSTS),
        help="Limit watcher delivery to signals for one host.",
    )
    watcher_parser.add_argument(
        "--target-thread-id",
        default=None,
    )
    watcher_parser.add_argument(
        "--action",
        choices=sorted(watcher.WATCHER_ACTIONS),
        default=None,
        help=(
            "Delivery action (default: notify; service restart inherits the "
            "stored action when omitted)."
        ),
    )
    watcher_subparsers = watcher_parser.add_subparsers(
        dest="watcher_command",
        required=True,
    )
    watcher_subparsers.add_parser("once", help="Run a single watcher scan and exit.")
    acknowledge = watcher_subparsers.add_parser(
        "acknowledge",
        help="Record an audit-preserving manual acknowledgement for one host.",
    )
    acknowledge_group = acknowledge.add_mutually_exclusive_group(required=True)
    acknowledge_group.add_argument("--event-id")
    acknowledge_group.add_argument(
        "--all-pending",
        action="store_true",
        help="Acknowledge every currently pending signal for the selected host.",
    )
    acknowledge.add_argument(
        "--confirm-all-pending",
        action="store_true",
        help="Required with --all-pending to make the bulk acknowledgement explicit.",
    )
    acknowledge.add_argument(
        "--reason",
        required=True,
        help="Human-readable manual-review reason retained in the receipt.",
    )
    deferred = watcher_subparsers.add_parser(
        "deferred",
        help="Inspect and operate on deferred watcher events.",
    )
    deferred_subparsers = deferred.add_subparsers(
        dest="deferred_command",
        required=True,
    )
    deferred_subparsers.add_parser(
        "list",
        help="List deferred watcher events without requiring a running service.",
    )
    deferred_retry = deferred_subparsers.add_parser(
        "retry",
        help="Re-arm a deferred watcher event for retry on the next scan.",
    )
    deferred_retry.add_argument("--event-id", required=True)
    deferred_retry.add_argument(
        "--reason",
        help="Human-readable retry note.",
    )
    watch = watcher_subparsers.add_parser(
        "watch",
        help="Run the watcher scan loop in the foreground.",
    )
    watch.add_argument("--interval-seconds", type=float, default=30)
    watch.add_argument("--heartbeat-file", type=Path)
    stream = watcher_subparsers.add_parser(
        "stream",
        help=(
            "Print one JSON line per new inbox signal; arm a host-native "
            "watch (e.g. a Claude session Monitor) on this command."
        ),
    )
    stream.add_argument("--interval-seconds", type=float, default=2)
    stream_subparsers = stream.add_subparsers(dest="stream_command")
    stream_subparsers.add_parser(
        "status",
        help="Report foreground stream health from its state file.",
    )
    service = watcher_subparsers.add_parser(
        "service",
        help="Control a detached background watcher process.",
    )
    service.add_argument("--service-file", type=Path)
    service_subparsers = service.add_subparsers(
        dest="service_command",
        required=True,
    )
    service_start = service_subparsers.add_parser(
        "start",
        help="Start a detached watcher process.",
    )
    service_start.add_argument("--interval-seconds", type=float, default=5)
    service_start.add_argument("--replace", action="store_true")
    service_subparsers.add_parser(
        "status",
        help="Report watcher process health and pending inbox count.",
    )
    service_stop = service_subparsers.add_parser(
        "stop",
        help="Stop a running watcher process.",
    )
    service_stop.add_argument("--timeout-seconds", type=float, default=5)
    service_restart = service_subparsers.add_parser(
        "restart",
        help="Stop and start the watcher process.",
    )
    service_restart.add_argument("--interval-seconds", type=float, default=None)
    service_restart.add_argument("--timeout-seconds", type=float, default=5)
    return parser


def project_roots(args: argparse.Namespace) -> list[Path]:
    roots = args.project_root or [Path.cwd()]
    return [root.expanduser().resolve() for root in roots]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = project_roots(args)
    try:
        if args.command == "emit":
            if len(roots) != 1:
                raise core.OrchestratorError("emit requires exactly one project root")
            output = core.write_terminal_event(
                roots[0],
                task_id=args.task_id,
                terminal_status=args.terminal_status,
                result_path=args.result,
                evidence_path=args.evidence,
                state_dir=args.state_dir,
                event_id=args.event_id,
            )
            print_json(output)
        elif args.command == "inbox":
            output = {
                str(root): core.inbox(root, state_dir=args.state_dir) for root in roots
            }
            print_json(output)
        elif args.command == "host-capabilities":
            print_json(host_capabilities.all_hosts())
        elif args.command == "runtime-capabilities":
            print_json(platform_runtime.capabilities())
        elif args.command == "codex":
            if len(roots) != 1:
                raise core.OrchestratorError(
                    "codex diagnose requires exactly one project root"
                )
            codex_command = args.codex_command
            launcher_source = "explicit" if codex_command else "path"
            if codex_command is None:
                bound = binding.load_binding(roots[0], state_dir=args.state_dir)
                if bound is not None and bound.get("host") == "codex":
                    configured = bound.get("codex_command")
                    if isinstance(configured, str):
                        codex_command = codex_app.resolve_codex_launcher(
                            configured, "codex"
                        )
                        launcher_source = "binding"
            codex_command = codex_command or "codex"
            output = codex_app.diagnose_codex_host(
                codex_command,
                timeout_seconds=args.timeout_seconds,
            )
            output["launcher_source"] = launcher_source
            print_json(output)
            return codex_app.codex_diagnostic_exit_code(output)
        elif args.command == "conformance":
            output = conformance.run_conformance(
                mode=args.mode,
                fixture_root=args.fixture_root,
                keep_fixture=args.keep_fixture,
                timeout_seconds=args.timeout_seconds,
            )
            print_json(output)
            return 0 if output["status"] == "passed" else 1
        elif args.command == "schemas":
            print_json(
                schemas.catalog() if args.name is None else schemas.load(args.name)
            )
        elif args.command == "cleanup":
            if len(roots) != 1:
                raise core.OrchestratorError(
                    "cleanup requires exactly one project root"
                )
            output = core.cleanup(
                roots[0],
                state_dir=args.state_dir,
                retention_days=args.retention_days,
                log_max_bytes=args.log_max_bytes,
                log_keep_bytes=args.log_keep_bytes,
                dry_run=args.dry_run,
            )
            print_json(output)
        elif args.command == "checks":
            if len(roots) != 1:
                raise core.OrchestratorError("checks requires exactly one project root")
            output = verification.checks_status(
                roots[0],
                state_dir=args.state_dir,
                check_id=args.check_id,
                status=args.status,
                minimum_severity=args.severity,
                large_log_bytes=args.large_log_bytes,
            )
            print_json(output)
            return worker_diagnostics.exit_code_for_worst(
                output.get("worst_severity") if isinstance(output, dict) else None
            )
        elif args.command == "check":
            if len(roots) != 1:
                raise core.OrchestratorError("check requires exactly one project root")
            output = run_local_check_command(args, roots[0])
            print_json(output)
            if (
                args.check_command in {"run", "supervise"}
                and output.get("status") in {"failed", "errored", "cancelled"}
            ):
                return 1
        elif args.command == "workstream":
            if len(roots) != 1:
                raise core.OrchestratorError(
                    "workstream requires exactly one project root"
                )
            print_json(run_workstream_command(args, roots[0]))
        elif args.command == "ci":
            if len(roots) != 1:
                raise core.OrchestratorError("ci requires exactly one project root")
            output = run_ci_command(args, roots[0])
            print_json(output)
        elif args.command == "pr":
            if len(roots) != 1:
                raise core.OrchestratorError("pr requires exactly one project root")
            print_json(run_pr_command(args, roots[0]))
        elif args.command == "doctor":
            if len(roots) != 1:
                raise core.OrchestratorError("doctor requires exactly one project root")
            output = diagnostics.run_doctor(
                roots[0],
                state_dir=args.state_dir,
                host=args.host,
            )
            print_json(output)
            return diagnostics.doctor_exit_code(output, strict=args.strict)
        elif args.command == "artifact":
            if len(roots) != 1:
                raise core.OrchestratorError(
                    "artifact requires exactly one project root"
                )
            if args.artifact_command == "resolve":
                output = artifact_resolution.write_resolution(
                    roots[0],
                    artifact_path=args.path,
                    reason=args.reason,
                    state_dir=args.state_dir,
                )
            else:
                output = artifact_resolution.list_resolutions(
                    roots[0],
                    state_dir=args.state_dir,
                )
            print_json(output)
        elif args.command == "status":
            if len(roots) != 1:
                raise core.OrchestratorError("status requires exactly one project root")
            output = status.run_status(
                roots[0],
                state_dir=args.state_dir,
                host=args.host,
                minimum_severity=args.severity,
                stale_after_seconds=args.stale_after_seconds,
                large_log_bytes=args.large_log_bytes,
                since_cursor=args.since_cursor,
            )
            print_json(output)
            return status.exit_code(output)
        elif args.command == "operation":
            if len(roots) != 1:
                raise core.OrchestratorError(
                    "operation commands require exactly one project root"
                )
            if args.operation_command == "status":
                output = operation_wait.operation_wait_snapshot(
                    roots[0],
                    targets=args.target,
                    mode=args.mode,
                    state_dir=args.state_dir,
                    stale_after_seconds=args.stale_after_seconds,
                )
                print_json(output)
                return operation_status_exit_code(output)
            output = run_operation_wait_command(args, roots[0])
            if args.json:
                print_json(output)
            return operation_wait_exit_code(output)
        elif args.command == "report":
            if len(roots) != 1:
                raise core.OrchestratorError("report requires exactly one project root")
            output = run_report_command(args, roots[0])
            if output is not None:
                print(output, end="")
        elif args.command == "adopt":
            if len(roots) != 1:
                raise core.OrchestratorError("adopt requires exactly one project root")
            output = adoption.adopt_project(
                roots[0],
                state_dir=args.state_dir,
                host=args.host,
                dry_run=args.dry_run,
            )
            print_json(output)
        elif args.command == "upgrade":
            if len(roots) != 1:
                raise core.OrchestratorError(
                    "upgrade requires exactly one project root"
                )
            output = upgrade.run_upgrade_check(
                roots[0],
                state_dir=args.state_dir,
                host=args.host,
            )
            print_json(output)
            return upgrade.exit_code(output, strict=args.strict)
        elif args.command == "bind":
            if len(roots) != 1:
                raise core.OrchestratorError("bind requires exactly one project root")
            output = run_bind_command(args, roots[0])
            print_json(output)
        elif args.command == "worker":
            if len(roots) != 1:
                raise core.OrchestratorError("worker requires exactly one project root")
            output = run_worker_cli_command(args, roots[0])
            if args.worker_command != "wait" or args.json:
                print_json(output)
            if args.worker_command == "wait":
                return worker_wait_exit_code(output)
            if args.worker_command in {"diagnose", "tasks"}:
                return worker_diagnostics.exit_code_for_worst(
                    output.get("worst_severity") if isinstance(output, dict) else None
                )
        elif args.command == "watcher":
            output = run_watcher_command(args, roots)
            if output is not None:
                print_json(output)
        else:  # pragma: no cover - argparse enforces this branch.
            raise core.OrchestratorError(f"unsupported command: {args.command}")
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


def run_bind_command(args: argparse.Namespace, root: Path) -> object:
    if args.status:
        bound = binding.load_binding(root, state_dir=args.state_dir)
        if bound is None:
            return {
                "schema_version": core.SCHEMA_VERSION,
                "kind": binding.BINDING_KIND,
                "status": "absent",
                "binding_path": str(
                    binding.binding_path(root, state_dir=args.state_dir)
                ),
            }
        return bound
    if args.clear:
        return binding.clear_binding(root, state_dir=args.state_dir)
    if not args.host:
        raise binding.BindingError("bind requires --host, --status or --clear")
    thread_id = args.thread_id
    detection_source = "explicit" if thread_id else None
    codex_command = args.codex_command
    if args.host == "codex":
        if not thread_id:
            detected = codex_app.detect_thread_id(root)
            if detected is None:
                raise binding.BindingError(
                    "could not auto-detect the codex thread id: Codex has no "
                    "stable thread-list interface and no matching legacy "
                    "rollout was found; the rollout may be absent or migrated. "
                    "Run this from inside the Codex chat being bound, or pass "
                    "--thread-id explicitly"
                )
            thread_id = detected["thread_id"]
            detection_source = detected["source"]
        if not codex_command:
            # Desktop threads live in the Windows-side session store and are
            # only reachable through codex.exe; derive the launcher from
            # where the thread's rollout actually lives.
            source_path = (
                detection_source
                if detection_source not in (None, "env", "explicit")
                else None
            )
            if source_path is None:
                rollout = codex_app.locate_thread_rollout(thread_id)
                source_path = str(rollout) if rollout else None
            if source_path and source_path.startswith("/mnt/"):
                codex_command = codex_app.default_windows_codex()
    result = binding.write_binding(
        root,
        host=args.host,
        target_thread_id=thread_id,
        codex_command=codex_command,
        state_dir=args.state_dir,
    )
    if detection_source:
        result["thread_id_source"] = detection_source
        result["thread_id_evidence"] = (
            "explicit"
            if detection_source == "explicit"
            else "environment"
            if detection_source == "env"
            else "legacy_rollout_heuristic"
        )
    return result


def run_workstream_command(args: argparse.Namespace, root: Path) -> object:
    if args.workstream_command == "start":
        return workstreams.start_workstream(
            root,
            workstream_id=args.workstream_id,
            goal=args.goal,
            state_dir=args.state_dir,
            delay_seconds=args.delay_seconds,
            max_continuations=args.max_continuations,
            max_wall_seconds=args.max_wall_seconds,
        )
    if args.workstream_command == "checkpoint":
        return workstreams.checkpoint_workstream(
            root,
            workstream_id=args.workstream_id,
            checkpoint_id=args.checkpoint_id,
            decision=args.decision,
            summary=args.summary,
            next_action=args.next_action,
            waiting_on=args.waiting_on,
            ready=args.ready,
            state_dir=args.state_dir,
        )
    if args.workstream_command == "resume":
        return workstreams.resume_workstream(
            root,
            workstream_id=args.workstream_id,
            state_dir=args.state_dir,
        )
    return workstreams.workstream_status(
        root,
        workstream_id=args.workstream_id,
        state_dir=args.state_dir,
    )


def run_local_check_command(args: argparse.Namespace, root: Path) -> dict:
    if args.check_command == "plan":
        return local_checks.plan_check(
            root,
            suite=args.suite,
            state_dir=args.state_dir,
            long_threshold_seconds=args.long_threshold_seconds,
        )
    if args.check_command == "run":
        return local_checks.start_check(
            root,
            check_id=args.check_id,
            suite=args.suite,
            state_dir=args.state_dir,
            execution=args.execution,
            wake_policy=args.wake_policy,
            long_threshold_seconds=args.long_threshold_seconds,
        )
    if args.check_command == "supervise":
        return local_checks.supervise_check(
            root,
            check_id=args.check_id,
            state_dir=args.state_dir,
        )
    if args.check_command == "reap":
        return local_checks.reap_checks(
            root,
            check_id=args.check_id,
            state_dir=args.state_dir,
        )
    return local_checks.check_status(
        root,
        check_id=args.check_id,
        state_dir=args.state_dir,
    )


def run_ci_command(args: argparse.Namespace, root: Path) -> object:
    if args.ci_command == "watch":
        return github_actions.start_monitor(
            root,
            repository=args.repo,
            run_id=args.run_id,
            state_dir=args.state_dir,
            hostname=args.hostname,
            attempt=args.attempt,
            expected_head_sha=args.expected_head_sha,
            workflow_name=args.workflow_name,
            gh_command=args.gh_command,
            timeout_seconds=args.timeout_seconds,
            wake_policy=args.wake_policy,
        )
    if args.ci_command == "status":
        return github_actions.monitor_status(
            root,
            state_dir=args.state_dir,
            monitor_id=args.monitor_id,
        )
    if args.ci_command == "cancel":
        return github_actions.cancel_monitor(
            root,
            monitor_id=args.monitor_id,
            reason=args.reason,
            state_dir=args.state_dir,
        )
    if args.ci_command == "retry":
        return github_actions.retry_monitor(
            root,
            monitor_id=args.monitor_id,
            reason=args.reason,
            state_dir=args.state_dir,
        )
    if args.ci_command == "reap":
        return github_actions.reap_monitors(root, state_dir=args.state_dir)
    if args.ci_command == "supervise":
        return github_actions.supervise_monitor(
            root,
            monitor_id=args.monitor_id,
            state_dir=args.state_dir,
        )
    raise github_actions.GitHubActionsError(
        f"unsupported ci command: {args.ci_command}"
    )


def run_pr_command(args: argparse.Namespace, root: Path) -> object:
    if args.pr_command == "watch":
        return github_pull_requests.start_monitor(
            root,
            repository=args.repo,
            pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            state_dir=args.state_dir,
            hostname=args.hostname,
            review_policy=args.review_policy,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
            wake_policy=args.wake_policy,
            gh_command=args.gh_command,
        )
    if args.pr_command == "status":
        return github_pull_requests.monitor_status(
            root,
            state_dir=args.state_dir,
            monitor_id=args.monitor_id,
        )
    if args.pr_command == "cancel":
        return github_pull_requests.cancel_monitor(
            root,
            monitor_id=args.monitor_id,
            reason=args.reason,
            state_dir=args.state_dir,
        )
    if args.pr_command == "retry":
        return github_pull_requests.retry_monitor(
            root,
            monitor_id=args.monitor_id,
            reason=args.reason,
            state_dir=args.state_dir,
        )
    if args.pr_command == "reap":
        return github_pull_requests.reap_monitors(root, state_dir=args.state_dir)
    if args.pr_command == "supervise":
        return github_pull_requests.supervise_monitor(
            root,
            monitor_id=args.monitor_id,
            state_dir=args.state_dir,
        )
    raise github_pull_requests.GitHubPullRequestError(
        f"unsupported pr command: {args.pr_command}"
    )


def run_worker_cli_command(args: argparse.Namespace, root: Path) -> object:
    if args.worker_command == "list":
        return workers.list_workers(root, state_dir=args.state_dir)
    if args.worker_command == "availability":
        return workers.availability_workers(
            root,
            state_dir=args.state_dir,
            worker=args.worker,
            enabled_only=not args.all,
        )
    if args.worker_command == "diagnose":
        return workers.diagnose_workers(
            root,
            state_dir=args.state_dir,
            worker=args.worker,
            minimum_severity=args.severity,
            enabled_only=args.enabled_only,
        )
    if args.worker_command == "policy" and args.worker_policy_command == "export":
        return worker_policy.export_bundled_policy(
            args.name,
            output=args.output,
            replace=args.replace,
        )
    if args.worker_command == "tasks":
        return task_diagnostics.diagnose_tasks(
            root,
            state_dir=args.state_dir,
            task_id=args.task_id,
            worker=args.worker,
            status=args.status,
            minimum_severity=args.severity,
            stale_after_seconds=args.stale_after_seconds,
            large_log_bytes=args.large_log_bytes,
        )
    if args.worker_command == "wait":
        return run_worker_wait_command(args, root)
    if args.worker_command == "resolve":
        return task_resolution.write_resolution(
            root,
            task_id=args.task_id,
            status=args.status,
            reason=args.reason,
            superseded_by_task_id=args.superseded_by_task_id,
            diagnostic_codes=args.diagnostic_code,
            state_dir=args.state_dir,
            replace=args.replace,
        )
    if args.worker_command == "resolutions":
        return task_resolution.list_resolutions(root, state_dir=args.state_dir)
    if args.worker_command == "reap":
        return workers.reap_worker_tasks(root, state_dir=args.state_dir)
    if args.worker_command == "queue" and args.worker_queue_command == "tick":
        return workers.queue_tick(root, state_dir=args.state_dir)
    if args.worker_command == "cancel":
        return workers.cancel_worker_task(
            root,
            task_id=args.task_id,
            mode=args.mode,
            reason=args.reason,
            state_dir=args.state_dir,
        )
    if args.worker_command == "run":
        return workers.run_worker(
            root,
            worker=args.worker,
            task_id=args.task_id,
            prompt_file=args.prompt_file,
            state_dir=args.state_dir,
            preflight_availability=args.preflight_availability,
            availability_mode=args.availability_mode,
            wake_policy=args.wake_policy,
            intent_file=args.intent_file,
            allow_duplicate=args.allow_duplicate,
            duplicate_reason=args.duplicate_reason,
        )
    if args.worker_command == "retry":
        return workers.retry_worker_task(
            root,
            task_id=args.task_id,
            new_task_id=args.new_task_id,
            max_attempts=args.max_attempts,
            delay_seconds=args.delay_seconds,
            reason=args.reason,
            state_dir=args.state_dir,
        )
    if args.worker_command == "supervise":
        return workers.supervise_worker(
            root,
            worker=args.worker,
            task_id=args.task_id,
            prompt_file=args.prompt_file,
            state_dir=args.state_dir,
        )
    raise workers.WorkerError(f"unsupported worker command: {args.worker_command}")


def run_worker_wait_command(args: argparse.Namespace, root: Path) -> dict[str, object]:
    interactive = not args.json
    is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    use_color = (interactive and args.color == "always") or (
        interactive
        and args.color == "auto"
        and is_tty
        and "NO_COLOR" not in os.environ
    )
    use_bell = (interactive and args.bell == "always") or (
        interactive and args.bell == "auto" and is_tty
    )
    last_line_width = 0

    def render(snapshot: dict[str, object]) -> None:
        nonlocal last_line_width
        final = bool(snapshot.get("terminal")) or (
            snapshot.get("wait_status") in {"timed_out", "action_required"}
        )
        if not is_tty and not final:
            return
        line = format_worker_wait_line(snapshot, use_color=use_color)
        if is_tty:
            padding = " " * max(last_line_width - visible_text_length(line), 0)
            print(f"\r{line}{padding}", end="\n" if final else "", flush=True)
            last_line_width = visible_text_length(line)
        else:
            print(line, flush=True)
        if final and use_bell:
            print("\a", end="", flush=True)

    wait_options = {
        "state_dir": args.state_dir,
        "interval_seconds": args.interval_seconds,
        "timeout_seconds": args.timeout_seconds,
        "stale_after_seconds": args.stale_after_seconds,
        "on_update": None if args.json else render,
    }
    if len(args.task_id) == 1:
        return workers.wait_for_worker_task(
            root,
            task_id=args.task_id[0],
            **wait_options,
        )
    return workers.wait_for_worker_tasks(
        root,
        task_ids=args.task_id,
        mode=args.mode,
        **wait_options,
    )


def run_operation_wait_command(
    args: argparse.Namespace, root: Path
) -> dict[str, object]:
    interactive = not args.json
    is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    use_color = (interactive and args.color == "always") or (
        interactive
        and args.color == "auto"
        and is_tty
        and "NO_COLOR" not in os.environ
    )
    use_bell = (interactive and args.bell == "always") or (
        interactive and args.bell == "auto" and is_tty
    )
    last_line_width = 0

    def render(snapshot: dict[str, object]) -> None:
        nonlocal last_line_width
        final = bool(snapshot.get("condition_met")) or snapshot.get(
            "wait_status"
        ) in {"timed_out", "action_required"}
        if not is_tty and not final:
            return
        line = format_operation_wait_line(snapshot, use_color=use_color)
        if is_tty:
            padding = " " * max(last_line_width - visible_text_length(line), 0)
            print(f"\r{line}{padding}", end="\n" if final else "", flush=True)
            last_line_width = visible_text_length(line)
        else:
            print(line, flush=True)
        if final and use_bell:
            print("\a", end="", flush=True)

    return operation_wait.wait_for_operations(
        root,
        targets=args.target,
        mode=args.mode,
        state_dir=args.state_dir,
        interval_seconds=args.interval_seconds,
        timeout_seconds=args.timeout_seconds,
        stale_after_seconds=args.stale_after_seconds,
        on_update=None if args.json else render,
    )


def operation_wait_exit_code(snapshot: dict[str, object]) -> int:
    if snapshot.get("wait_status") == "timed_out":
        return 124
    if snapshot.get("wait_status") == "action_required":
        return 3
    return 0 if snapshot.get("status") == "completed" else 2


def operation_status_exit_code(snapshot: dict[str, object]) -> int:
    if snapshot.get("wait_status") == "action_required":
        return 3
    if snapshot.get("condition_met") and snapshot.get("status") != "completed":
        return 2
    return 0


def format_operation_wait_line(
    snapshot: dict[str, object], *, use_color: bool
) -> str:
    status = str(snapshot.get("status") or "unknown")
    mode = str(snapshot.get("mode") or "all")
    terminal_count = int(snapshot.get("terminal_count") or 0)
    target_count = int(snapshot.get("target_count") or 0)
    successful_count = int(snapshot.get("successful_count") or 0)
    waited = float(snapshot.get("waited_seconds") or 0.0)
    if snapshot.get("wait_status") == "action_required":
        label, color = "ACTION", "31"
        count = int(snapshot.get("action_required_count") or 0)
        detail = f"{count} operation(s) need review; return to the chat"
    elif snapshot.get("wait_status") == "timed_out":
        label, color = "WAIT", "33"
        detail = "operation set still active; re-run this command later"
    elif snapshot.get("condition_met") and status == "completed":
        label, color = "DONE", "32"
        detail = "return to the chat to review results"
    elif snapshot.get("condition_met"):
        label, color = "ACTION", "31"
        detail = "return to the chat to review unsuccessful results"
    else:
        label, color = "WORKING", "36"
        detail = f"waiting {waited:.0f}s"
    prefix = f"[{label}]"
    if use_color:
        prefix = f"\x1b[{color};1m{prefix}\x1b[0m"
    return (
        f"{prefix} {terminal_count}/{target_count} operations | {mode} | "
        f"{successful_count} successful | {detail}"
    )


def worker_wait_exit_code(snapshot: dict[str, object]) -> int:
    if snapshot.get("wait_status") == "timed_out":
        return 124
    if snapshot.get("wait_status") == "action_required":
        return 3
    return 0 if snapshot.get("status") == "completed" else 2


def format_worker_wait_line(
    snapshot: dict[str, object], *, use_color: bool
) -> str:
    if snapshot.get("kind") == "WORKER_WAIT_GROUP_STATUS":
        return format_worker_wait_group_line(snapshot, use_color=use_color)
    status = str(snapshot.get("status") or "unknown")
    task_id = str(snapshot.get("task_id") or "unknown")
    worker = str(snapshot.get("worker") or "unknown")
    waited = float(snapshot.get("waited_seconds") or 0.0)
    if snapshot.get("wait_status") == "action_required":
        label, color = "ACTION", "31"
        health = snapshot.get("health")
        health_status = (
            str(health.get("status")) if isinstance(health, dict) else "unhealthy"
        )
        detail = f"{health_status}; return to the chat"
    elif snapshot.get("wait_status") == "timed_out":
        label, color = "WAIT", "33"
        detail = "still active; re-run this command later"
    elif status == "completed":
        label, color = "DONE", "32"
        detail = "return to the chat to review the result"
    elif status in core.TERMINAL_STATUSES:
        label, color = "ACTION", "31"
        detail = "return to the chat to review diagnostics"
    else:
        label, color = "WORKING", "36"
        detail = f"waiting {waited:.0f}s"
    prefix = f"[{label}]"
    if use_color:
        prefix = f"\x1b[{color};1m{prefix}\x1b[0m"
    return f"{prefix} {task_id} | {worker} | {status} | {detail}"


def format_worker_wait_group_line(
    snapshot: dict[str, object], *, use_color: bool
) -> str:
    status = str(snapshot.get("status") or "unknown")
    mode = str(snapshot.get("mode") or "all")
    terminal_count = int(snapshot.get("terminal_count") or 0)
    task_count = int(snapshot.get("task_count") or 0)
    waited = float(snapshot.get("waited_seconds") or 0.0)
    if snapshot.get("wait_status") == "action_required":
        label, color = "ACTION", "31"
        count = int(snapshot.get("action_required_count") or 0)
        detail = f"{count} task(s) need review; return to the chat"
    elif snapshot.get("wait_status") == "timed_out":
        label, color = "WAIT", "33"
        detail = "task set still active; re-run this command later"
    elif snapshot.get("condition_met") and status == "completed":
        label, color = "DONE", "32"
        detail = "return to the chat to review results"
    elif snapshot.get("condition_met"):
        label, color = "ACTION", "31"
        detail = "return to the chat to review unsuccessful results"
    else:
        label, color = "WORKING", "36"
        detail = f"waiting {waited:.0f}s"
    prefix = f"[{label}]"
    if use_color:
        prefix = f"\x1b[{color};1m{prefix}\x1b[0m"
    return (
        f"{prefix} {terminal_count}/{task_count} tasks | {mode} | "
        f"{status} | {detail}"
    )


def visible_text_length(value: str) -> int:
    length = 0
    in_escape = False
    for character in value:
        if character == "\x1b":
            in_escape = True
        elif in_escape and character == "m":
            in_escape = False
        elif not in_escape:
            length += 1
    return length


def run_report_command(args: argparse.Namespace, root: Path) -> str | None:
    if args.report_command == "draft":
        draft = status.report_draft(
            root,
            state_dir=args.state_dir,
            project_name=args.project_name,
            report_type=args.type,
            host=args.host,
            minimum_severity=args.severity,
            stale_after_seconds=args.stale_after_seconds,
            large_log_bytes=args.large_log_bytes,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(draft, encoding="utf-8")
            return None
        return draft
    raise core.OrchestratorError(f"unsupported report command: {args.report_command}")


def run_watcher_command(args: argparse.Namespace, roots: list[Path]) -> object | None:
    restarting = (
        args.watcher_command == "service" and args.service_command == "restart"
    )
    action = args.action if restarting else (args.action or "notify")
    target_thread_id = watcher_target_thread_id(args, action=action)
    host_filter = {args.host} if args.host else None
    # Operator commands must reach the same state file the service uses:
    # host-scoped callback services keep their deferred events in
    # watcher-<host>-callback-state.json, not the legacy watcher-state.json.
    operator_state = args.state_file
    if operator_state is None and args.host:
        operator_state = watcher.default_host_state_path(
            roots[0],
            host=args.host,
            state_dir=args.state_dir,
        )
    if args.watcher_command == "once":
        return watcher.scan_once(
            roots,
            state_dir=args.state_dir,
            state_path=args.state_file,
            action=action,
            target_thread_id=target_thread_id,
            codex=args.codex,
            host_filter=host_filter,
        )
    if args.watcher_command == "acknowledge":
        if len(roots) != 1:
            raise watcher.WatcherError("acknowledge requires exactly one project root")
        if not args.host:
            raise watcher.WatcherError("acknowledge requires an explicit --host")
        if args.all_pending:
            if not args.confirm_all_pending:
                raise watcher.WatcherError(
                    "--all-pending requires --confirm-all-pending"
                )
            return watcher.acknowledge_pending_signals(
                roots[0],
                host=args.host,
                state_dir=args.state_dir,
                state_path=operator_state,
                reason=args.reason,
            )
        return watcher.acknowledge_signal(
            roots[0],
            event_id=args.event_id,
            host=args.host,
            state_dir=args.state_dir,
            state_path=operator_state,
            reason=args.reason,
        )
    if args.watcher_command == "deferred":
        if args.deferred_command == "list":
            return watcher.list_deferred_events(
                roots,
                state_dir=args.state_dir,
                state_path=operator_state,
            )
        if args.deferred_command == "retry":
            if len(roots) != 1:
                raise watcher.WatcherError(
                    "deferred retry requires exactly one project root"
                )
            return watcher.retry_deferred_event(
                roots[0],
                event_id=args.event_id,
                state_dir=args.state_dir,
                state_path=operator_state,
                reason=args.reason,
            )
        raise watcher.WatcherError(
            f"unsupported deferred command: {args.deferred_command}"
        )
    if args.watcher_command == "watch":
        watcher.watch(
            roots,
            state_dir=args.state_dir,
            interval_seconds=args.interval_seconds,
            state_path=args.state_file,
            action=action,
            target_thread_id=target_thread_id,
            codex=args.codex,
            heartbeat_file=args.heartbeat_file,
            host_filter=host_filter,
        )
        return None
    if args.watcher_command == "stream":
        if args.stream_command == "status":
            return claude_stream.stream_status(
                roots,
                state_dir=args.state_dir,
                state_path=args.state_file,
                interval_seconds=args.interval_seconds,
            )
        claude_stream.stream_signals(
            roots,
            state_dir=args.state_dir,
            state_path=args.state_file,
            interval_seconds=args.interval_seconds,
        )
        return None
    if args.watcher_command != "service":
        raise watcher.WatcherError(
            f"unsupported watcher command: {args.watcher_command}"
        )
    if args.service_command == "start":
        return watcher.start_service(
            roots,
            state_dir=args.state_dir,
            interval_seconds=args.interval_seconds,
            state_path=args.state_file,
            service_file=args.service_file,
            action=action,
            target_thread_id=target_thread_id,
            codex=args.codex,
            host=args.host,
            replace=args.replace,
        )
    if args.service_command == "status":
        return watcher.service_status(
            roots,
            state_dir=args.state_dir,
            service_file=args.service_file,
            host=args.host,
        )
    if args.service_command == "stop":
        return watcher.stop_service(
            roots,
            state_dir=args.state_dir,
            service_file=args.service_file,
            host=args.host,
            timeout_seconds=args.timeout_seconds,
        )
    if args.service_command == "restart":
        return watcher.restart_service(
            roots,
            state_dir=args.state_dir,
            interval_seconds=args.interval_seconds,
            state_path=args.state_file,
            service_file=args.service_file,
            action=action,
            target_thread_id=target_thread_id,
            codex=args.codex,
            host=args.host,
            timeout_seconds=args.timeout_seconds,
        )
    raise watcher.WatcherError(f"unsupported service command: {args.service_command}")


def watcher_target_thread_id(
    args: argparse.Namespace,
    *,
    action: str | None = None,
) -> str | None:
    if args.target_thread_id:
        return args.target_thread_id
    if (action or args.action) == "current-thread-callback":
        return os.environ.get("CODEX_THREAD_ID")
    return None


if __name__ == "__main__":
    sys.exit(main())
