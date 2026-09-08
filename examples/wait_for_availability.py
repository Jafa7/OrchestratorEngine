#!/usr/bin/env python3
"""Wait for an adopter-owned non-AI probe; run under the existing worker runner."""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path

from orchestrator_engine import core, worker_diagnostics, workers


def wait_for_availability(
    project_root: Path,
    *,
    worker: str,
    retry_seconds: float,
    maximum_retry_seconds: float,
    not_before: datetime | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict:
    for value in (retry_seconds, maximum_retry_seconds):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("retry intervals must be finite and positive")
    if maximum_retry_seconds < retry_seconds:
        raise ValueError(
            "maximum retry interval must not be smaller than retry interval"
        )
    if not_before is not None:
        if not_before.tzinfo is None:
            raise ValueError("not_before must include a timezone")
        delay = (not_before - datetime.now(UTC)).total_seconds()
        if delay > 0:
            time.sleep(delay)
    attempts = 0
    delay = retry_seconds
    while True:
        # Reload on every probe so disabling/changing a profile takes effect.
        config = workers.require_worker(project_root, worker, state_dir=state_dir)
        result = worker_diagnostics.run_availability_probe(config)
        attempts += 1
        if result["status"] != "unavailable":
            return {
                "kind": "AVAILABILITY_WAIT_RESULT",
                "worker": worker,
                "status": result["status"],
                "probe_count": attempts,
                "checked_at": core.utc_now(),
            }
        # This is deterministic process waiting, never an agent/model retry.
        time.sleep(delay)
        delay = min(maximum_retry_seconds, delay * 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--state-dir", default=core.DEFAULT_STATE_DIR)
    parser.add_argument("--retry-seconds", required=True, type=float)
    parser.add_argument("--maximum-retry-seconds", required=True, type=float)
    parser.add_argument(
        "--not-before", help="Provider-supplied reset time, with timezone."
    )
    args = parser.parse_args()
    try:
        result = wait_for_availability(
            args.project_root,
            worker=args.worker,
            state_dir=args.state_dir,
            retry_seconds=args.retry_seconds,
            maximum_retry_seconds=args.maximum_retry_seconds,
            not_before=(
                datetime.fromisoformat(args.not_before.replace("Z", "+00:00"))
                if args.not_before
                else None
            ),
        )
    except (OSError, RuntimeError, ValueError) as error:
        result = {"status": "error", "reason": str(error)[:240]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "available" else 2


if __name__ == "__main__":
    raise SystemExit(main())
