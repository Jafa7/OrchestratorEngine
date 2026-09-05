#!/usr/bin/env python3
"""Repeat installed conformance runs and emit one bounded reliability report."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MAX_ITERATIONS = 1000


class ReliabilitySoakError(RuntimeError):
    """The soak configuration or a conformance response is invalid."""

    def __init__(self, message: str, *, failure_type: str = "invalid_output") -> None:
        super().__init__(message)
        self.failure_type = failure_type


def conformance_run(
    cli: Path,
    *,
    mode: str,
    timeout_seconds: float,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    outer_timeout = timeout_seconds + 15
    try:
        completed = subprocess.run(
            [
                str(cli),
                "conformance",
                "run",
                "--mode",
                mode,
                "--timeout-seconds",
                str(timeout_seconds),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=outer_timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ReliabilitySoakError(
            f"conformance process exceeded outer timeout {outer_timeout:.1f}s",
            failure_type="outer_timeout",
        ) from error
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        detail = (completed.stderr or completed.stdout).replace("\n", " ")[:500]
        raise ReliabilitySoakError(
            f"conformance returned invalid JSON: {detail}"
        ) from error
    if not isinstance(report, dict):
        raise ReliabilitySoakError("conformance returned non-object JSON")
    return completed, report


def run_soak(
    cli: Path,
    *,
    iterations: int,
    mode: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if iterations < 1 or iterations > MAX_ITERATIONS:
        raise ReliabilitySoakError(
            f"iterations must be between 1 and {MAX_ITERATIONS}"
        )
    if mode not in {"portable", "full"}:
        raise ReliabilitySoakError("mode must be portable or full")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ReliabilitySoakError("timeout must be finite and positive")

    started = time.monotonic()
    durations: list[float] = []
    failure: dict[str, Any] | None = None
    for iteration in range(1, iterations + 1):
        try:
            completed, report = conformance_run(
                cli,
                mode=mode,
                timeout_seconds=timeout_seconds,
            )
        except (OSError, ReliabilitySoakError) as error:
            failure = {
                "iteration": iteration,
                "exit_code": None,
                "type": (
                    "launch_error"
                    if isinstance(error, OSError)
                    else error.failure_type
                ),
                "message": str(error)[:500],
                "fixture": None,
            }
            break
        duration = report.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            durations.append(float(duration))
        if completed.returncode != 0 or report.get("status") != "passed":
            error = report.get("failure")
            failure = {
                "iteration": iteration,
                "exit_code": completed.returncode,
                "type": error.get("type") if isinstance(error, dict) else None,
                "message": (
                    str(error.get("message"))[:500]
                    if isinstance(error, dict) and error.get("message") is not None
                    else None
                ),
                "fixture": report.get("fixture"),
            }
            break

    completed_iterations = failure["iteration"] if failure is not None else iterations
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_RELIABILITY_SOAK_REPORT",
        "status": "failed" if failure is not None else "passed",
        "mode": mode,
        "iterations_requested": iterations,
        "iterations_completed": completed_iterations,
        "duration_seconds": round(time.monotonic() - started, 3),
        "conformance_duration_seconds": {
            "minimum": round(min(durations), 3) if durations else None,
            "maximum": round(max(durations), 3) if durations else None,
            "average": round(sum(durations) / len(durations), 3)
            if durations
            else None,
        },
    }
    if failure is not None:
        report["failure"] = failure
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cli", type=Path, default=Path("orchestrator-engine")
    )
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--mode", choices=("portable", "full"), default="full")
    parser.add_argument("--timeout-seconds", type=float, default=15)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = run_soak(
            args.cli.expanduser(),
            iterations=args.iterations,
            mode=args.mode,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, ReliabilitySoakError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
