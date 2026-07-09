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
    binding,
    claude_stream,
    codex_app,
    core,
    diagnostics,
    watcher,
    workers,
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

    doctor = subparsers.add_parser(
        "doctor",
        help="Run read-only project health diagnostics.",
    )
    doctor.add_argument(
        "--host",
        choices=sorted(binding.SUPPORTED_HOSTS),
        help="Check the wake channel for one host instead of the bound host.",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="Return a non-zero exit code for warnings as well as errors.",
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

    bind = subparsers.add_parser(
        "bind",
        help="Declare which host chat the watcher should wake.",
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
    worker_run = worker_subparsers.add_parser(
        "run",
        help="Dispatch a task to a worker detached and return immediately.",
    )
    worker_run.add_argument("--worker", required=True)
    worker_run.add_argument("--task-id", required=True)
    worker_run.add_argument("--prompt-file", type=Path, required=True)
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
        default="notify",
    )
    watcher_subparsers = watcher_parser.add_subparsers(
        dest="watcher_command",
        required=True,
    )
    watcher_subparsers.add_parser("once", help="Run a single watcher scan and exit.")
    acknowledge = watcher_subparsers.add_parser(
        "acknowledge",
        help="Mark a pending or deferred terminal event as manually handled.",
    )
    acknowledge.add_argument("--event-id", required=True)
    acknowledge.add_argument(
        "--reason",
        help="Human-readable acknowledgement note.",
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
    service_restart.add_argument("--interval-seconds", type=float, default=5)
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
                str(root): core.inbox(root, state_dir=args.state_dir)
                for root in roots
            }
            print_json(output)
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
        elif args.command == "doctor":
            if len(roots) != 1:
                raise core.OrchestratorError(
                    "doctor requires exactly one project root"
                )
            output = diagnostics.run_doctor(
                roots[0],
                state_dir=args.state_dir,
                host=args.host,
            )
            print_json(output)
            return diagnostics.doctor_exit_code(output, strict=args.strict)
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
        elif args.command == "bind":
            if len(roots) != 1:
                raise core.OrchestratorError("bind requires exactly one project root")
            output = run_bind_command(args, roots[0])
            print_json(output)
        elif args.command == "worker":
            if len(roots) != 1:
                raise core.OrchestratorError(
                    "worker requires exactly one project root"
                )
            output = run_worker_cli_command(args, roots[0])
            print_json(output)
        elif args.command == "watcher":
            output = run_watcher_command(args, roots)
            if output is not None:
                print_json(output)
        else:  # pragma: no cover - argparse enforces this branch.
            raise core.OrchestratorError(f"unsupported command: {args.command}")
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
                    "could not auto-detect the codex thread id; run this "
                    "from inside the codex chat being bound, or pass "
                    "--thread-id"
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
    return result


def run_worker_cli_command(args: argparse.Namespace, root: Path) -> object:
    if args.worker_command == "list":
        return workers.list_workers(root, state_dir=args.state_dir)
    if args.worker_command == "run":
        return workers.run_worker(
            root,
            worker=args.worker,
            task_id=args.task_id,
            prompt_file=args.prompt_file,
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


def run_watcher_command(args: argparse.Namespace, roots: list[Path]) -> object | None:
    target_thread_id = watcher_target_thread_id(args)
    host_filter = {args.host} if args.host else None
    # Operator commands must reach the same state file the service uses:
    # host-scoped callback services keep their deferred events in
    # watcher-<host>-callback-state.json, not the legacy watcher-state.json.
    operator_state = args.state_file
    if operator_state is None and args.host:
        operator_state = watcher.default_callback_state_path(
            roots[0],
            host=args.host,
            state_dir=args.state_dir,
        )
    if args.watcher_command == "once":
        return watcher.scan_once(
            roots,
            state_dir=args.state_dir,
            state_path=args.state_file,
            action=args.action,
            target_thread_id=target_thread_id,
            codex=args.codex,
            host_filter=host_filter,
        )
    if args.watcher_command == "acknowledge":
        if len(roots) != 1:
            raise watcher.WatcherError("acknowledge requires exactly one project root")
        return watcher.acknowledge_deferred_event(
            roots[0],
            event_id=args.event_id,
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
            action=args.action,
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
            action=args.action,
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
        watcher.stop_service(
            roots,
            state_dir=args.state_dir,
            service_file=args.service_file,
            host=args.host,
            timeout_seconds=args.timeout_seconds,
        )
        return watcher.start_service(
            roots,
            state_dir=args.state_dir,
            interval_seconds=args.interval_seconds,
            state_path=args.state_file,
            service_file=args.service_file,
            action=args.action,
            target_thread_id=target_thread_id,
            codex=args.codex,
            host=args.host,
            replace=True,
        )
    raise watcher.WatcherError(f"unsupported service command: {args.service_command}")


def watcher_target_thread_id(args: argparse.Namespace) -> str | None:
    if args.target_thread_id:
        return args.target_thread_id
    if args.action == "current-thread-callback":
        return os.environ.get("CODEX_THREAD_ID")
    return None


if __name__ == "__main__":
    sys.exit(main())
