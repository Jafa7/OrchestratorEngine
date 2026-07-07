"""Command-line interface for OrchestratorEngine."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import core, watcher


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OrchestratorEngine")
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
        "--target-thread-id",
        default=os.environ.get("CODEX_THREAD_ID"),
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
    watch = watcher_subparsers.add_parser(
        "watch",
        help="Run the watcher scan loop in the foreground.",
    )
    watch.add_argument("--interval-seconds", type=float, default=30)
    watch.add_argument("--heartbeat-file", type=Path)
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


def run_watcher_command(args: argparse.Namespace, roots: list[Path]) -> object | None:
    if args.watcher_command == "once":
        return watcher.scan_once(
            roots,
            state_dir=args.state_dir,
            state_path=args.state_file,
            action=args.action,
            target_thread_id=args.target_thread_id,
            codex=args.codex,
        )
    if args.watcher_command == "watch":
        watcher.watch(
            roots,
            state_dir=args.state_dir,
            interval_seconds=args.interval_seconds,
            state_path=args.state_file,
            action=args.action,
            target_thread_id=args.target_thread_id,
            codex=args.codex,
            heartbeat_file=args.heartbeat_file,
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
            target_thread_id=args.target_thread_id,
            codex=args.codex,
            replace=args.replace,
        )
    if args.service_command == "status":
        return watcher.service_status(
            roots,
            state_dir=args.state_dir,
            service_file=args.service_file,
        )
    if args.service_command == "stop":
        return watcher.stop_service(
            roots,
            state_dir=args.state_dir,
            service_file=args.service_file,
            timeout_seconds=args.timeout_seconds,
        )
    if args.service_command == "restart":
        watcher.stop_service(
            roots,
            state_dir=args.state_dir,
            service_file=args.service_file,
            timeout_seconds=args.timeout_seconds,
        )
        return watcher.start_service(
            roots,
            state_dir=args.state_dir,
            interval_seconds=args.interval_seconds,
            state_path=args.state_file,
            service_file=args.service_file,
            action=args.action,
            target_thread_id=args.target_thread_id,
            codex=args.codex,
            replace=True,
        )
    raise watcher.WatcherError(f"unsupported service command: {args.service_command}")


if __name__ == "__main__":
    sys.exit(main())
