#!/usr/bin/env python3
"""Portable verification runner that writes compact orchestration artifacts.

This is intentionally an example runner, not OrchestratorEngine core logic.
Adopting projects may copy it, replace it, or wrap an existing native runner as
long as the emitted verification result follows the documented contract.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RESULT_KIND = "ORCHESTRATOR_VERIFICATION_RESULT"
DEFAULT_CONFIG = ".orchestrator/checks.toml"
DEFAULT_TAIL_LINES = 80


@dataclass(frozen=True)
class CommandSpec:
    label: str
    argv: list[str]
    cwd: Path
    required: bool = True
    timeout_seconds: float | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def safe_label(label: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in label.strip()
    ).strip("-")
    return cleaned or "command"


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def ensure_command(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("expected a command after -- or a configured suite")
    return command


def load_suite_commands(
    *,
    config_path: Path,
    suite: str,
    project_root: Path,
) -> list[CommandSpec]:
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    suites = config.get("suites")
    if not isinstance(suites, dict) or suite not in suites:
        raise SystemExit(f"suite {suite!r} not found in {config_path}")
    suite_config = suites[suite]
    if not isinstance(suite_config, dict):
        raise SystemExit(f"suite {suite!r} must be a table")
    raw_commands = suite_config.get("commands")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise SystemExit(f"suite {suite!r} must define commands")

    commands: list[CommandSpec] = []
    for index, raw in enumerate(raw_commands, start=1):
        if not isinstance(raw, dict):
            raise SystemExit(f"suite {suite!r} command {index} must be a table")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            raise SystemExit(
                f"suite {suite!r} command {index} must define argv as strings"
            )
        label = raw.get("label", f"command-{index}")
        if not isinstance(label, str):
            raise SystemExit(f"suite {suite!r} command {index} label must be a string")
        cwd_value = raw.get("cwd", ".")
        if not isinstance(cwd_value, str):
            raise SystemExit(f"suite {suite!r} command {index} cwd must be a string")
        timeout_value = raw.get("timeout_seconds")
        if timeout_value is not None and not isinstance(timeout_value, int | float):
            raise SystemExit(
                f"suite {suite!r} command {index} timeout_seconds must be numeric"
            )
        required = raw.get("required", True)
        if not isinstance(required, bool):
            raise SystemExit(f"suite {suite!r} command {index} required must be bool")
        commands.append(
            CommandSpec(
                label=label,
                argv=list(argv),
                cwd=(project_root / cwd_value).resolve(),
                required=required,
                timeout_seconds=(
                    float(timeout_value) if timeout_value is not None else None
                ),
            )
        )
    return commands


def build_commands(args: argparse.Namespace, project_root: Path) -> list[CommandSpec]:
    if args.suite:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = project_root / config_path
        return load_suite_commands(
            config_path=config_path,
            suite=args.suite,
            project_root=project_root,
        )
    return [
        CommandSpec(
            label=args.label,
            argv=ensure_command(list(args.command)),
            cwd=project_root,
            required=True,
            timeout_seconds=args.timeout_seconds,
        )
    ]


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


def read_lines(stream, lines: queue.Queue[str]) -> None:
    for line in stream:
        lines.put(line)


def run_command(
    spec: CommandSpec,
    *,
    project_root: Path,
    artifacts_dir: Path,
    full_log,
    tail_lines: int,
) -> dict[str, Any]:
    label_slug = safe_label(spec.label)
    log_path = artifacts_dir / f"{label_slug}.log"
    started_at = utc_now()
    started = time.monotonic()
    tail: deque[str] = deque(maxlen=tail_lines)
    line_count = 0
    timed_out = False
    error: str | None = None

    header = f"\n--- {spec.label}: {shell_join(spec.argv)} ---\n"
    full_log.write(header)
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
    except OSError as exc:
        duration = time.monotonic() - started
        error = str(exc)
        log_path.write_text(error + "\n", encoding="utf-8")
        full_log.write(error + "\n")
        full_log.flush()
        return {
            "label": spec.label,
            "required": spec.required,
            "status": "errored",
            "exit_code": None,
            "started_at": started_at,
            "finished_at": utc_now(),
            "duration_seconds": round(duration, 3),
            "cwd": relative_path(spec.cwd, project_root),
            "argv": spec.argv,
            "command": shell_join(spec.argv),
            "log_path": relative_path(log_path, project_root),
            "output_tail": [error],
            "output_line_count": 1,
            "error": error,
        }

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
            else:
                command_log.write(line)
                full_log.write(line)
                tail.append(line.rstrip("\n"))
                line_count += 1
    reader.join(timeout=1)
    exit_code = process.poll()
    if exit_code is None:
        terminate_process(process)
        exit_code = process.poll()
    full_log.flush()
    duration = time.monotonic() - started
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
        "finished_at": utc_now(),
        "duration_seconds": round(duration, 3),
        "cwd": relative_path(spec.cwd, project_root),
        "argv": spec.argv,
        "command": shell_join(spec.argv),
        "log_path": relative_path(log_path, project_root),
        "output_tail": list(tail),
        "output_line_count": line_count,
    }
    if timed_out:
        result["error"] = f"command timed out after {spec.timeout_seconds}s"
    elif error:
        result["error"] = error
    return result


def overall_status(command_results: list[dict[str, Any]]) -> str:
    required = [result for result in command_results if result.get("required", True)]
    if not required:
        return "passed"
    if any(result["status"] == "errored" for result in required):
        return "errored"
    if any(result["status"] in {"failed", "timed_out"} for result in required):
        return "failed"
    return "passed"


def build_summary(result: dict[str, Any]) -> str:
    lines = [
        f"Schema: {RESULT_KIND} v{SCHEMA_VERSION}",
        f"Status: {result['status']}",
        f"Check: {result['check_id']}",
        f"Suite: {result.get('suite') or '-'}",
        f"Duration: {result['duration_seconds']:.3f}s",
        "",
        "Commands:",
    ]
    for command in result["commands"]:
        lines.append(
            "- "
            f"{command['label']} [{command['status']}] "
            f"{command['duration_seconds']:.3f}s "
            f"exit={command['exit_code']} :: {command['command']}"
        )
    failing = [
        command
        for command in result["commands"]
        if command["required"] and command["status"] != "passed"
    ]
    if failing:
        lines.extend(["", "Failure excerpts:"])
        for command in failing:
            lines.append(f"[{command['label']}] {command['status']}")
            tail = command.get("output_tail") or []
            lines.extend(f"  {line}" for line in tail[-20:])
            if command.get("error"):
                lines.append(f"  error: {command['error']}")
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".", help="Project root.")
    parser.add_argument("--check-id", help="Stable id for this verification run.")
    parser.add_argument("--suite", help="Suite name in .orchestrator/checks.toml.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Suite TOML path.")
    parser.add_argument("--label", default="check", help="Single-command label.")
    parser.add_argument("--timeout-seconds", type=float, help="Single-command timeout.")
    parser.add_argument(
        "--tail-lines",
        type=int,
        default=DEFAULT_TAIL_LINES,
        help="Lines kept in result.json for each command.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the verification result JSON instead of the text summary.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    project_root = Path(args.project_root).expanduser().resolve()
    generated_id = (
        f"check-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    check_id = args.check_id or generated_id
    artifacts_dir = project_root / ".orchestrator" / "checks" / check_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    commands = build_commands(args, project_root)

    started_at = utc_now()
    started = time.monotonic()
    full_log_path = artifacts_dir / "full.log"
    with full_log_path.open("w", encoding="utf-8") as full_log:
        command_results = [
            run_command(
                command,
                project_root=project_root,
                artifacts_dir=artifacts_dir,
                full_log=full_log,
                tail_lines=max(args.tail_lines, 1),
            )
            for command in commands
        ]
    status = overall_status(command_results)
    finished_at = utc_now()
    duration = time.monotonic() - started
    result_path = artifacts_dir / "verification-result.json"
    summary_path = artifacts_dir / "summary.txt"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "check_id": check_id,
        "suite": args.suite,
        "status": status,
        "exit_code": 0 if status == "passed" else 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration, 3),
        "commands": command_results,
        "result_path": relative_path(result_path, project_root),
        "summary_path": relative_path(summary_path, project_root),
        "log_path": relative_path(full_log_path, project_root),
    }
    summary = build_summary(result)
    summary_path.write_text(summary, encoding="utf-8")
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(summary, end="")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
