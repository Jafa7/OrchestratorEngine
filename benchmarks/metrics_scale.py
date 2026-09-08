#!/usr/bin/env python3
"""Run an explicit synthetic metrics scale benchmark outside the test suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from pathlib import Path

from orchestrator_engine.metrics import contracts
from orchestrator_engine.metrics.reporting import build_report
from orchestrator_engine.metrics.store import MetricsStore


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--observations", type=int, default=100_000)
    value.add_argument("--attempts", type=int, default=10_000)
    value.add_argument("--packages", type=int, default=1_000)
    value.add_argument("--output", type=Path)
    value.add_argument(
        "--verification-command",
        nargs=argparse.REMAINDER,
        help="Optional argv run before and after collection for paired timing.",
    )
    return value


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def peak_memory_bytes() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes.
    return int(peak * 1024 if platform.system() == "Linux" else peak)


def source_revision(root: Path) -> dict[str, object]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    state = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    tracked_inputs = [
        root / "src" / "orchestrator_engine" / "cli.py",
        root / "src" / "orchestrator_engine" / "platform_runtime.py",
        root / "benchmarks" / "metrics_scale.py",
        *(root / "src" / "orchestrator_engine" / "metrics").glob("*.py"),
        *(root / "src" / "orchestrator_engine" / "schemas").glob("metrics-*.json"),
    ]
    source_hash = hashlib.sha256()
    for path in sorted(tracked_inputs):
        source_hash.update(str(path.relative_to(root)).encode("utf-8"))
        source_hash.update(b"\0")
        source_hash.update(path.read_bytes())
        source_hash.update(b"\0")
    return {
        "source_head": head.stdout.strip() if head.returncode == 0 else None,
        "source_state": (
            "working_tree_with_changes"
            if state.returncode == 0 and state.stdout.strip()
            else "clean"
            if state.returncode == 0
            else "unknown"
        ),
        "source_tree_sha256": source_hash.hexdigest(),
    }


def run_verification(argv: list[str] | None) -> dict[str, object]:
    if not argv:
        return {"status": "not_measured"}
    started = time.monotonic()
    completed = subprocess.run(argv, capture_output=True, check=False)
    return {
        "status": "completed",
        "exit_code": completed.returncode,
        "duration_seconds": time.monotonic() - started,
        "stdout_bytes": len(completed.stdout),
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_bytes": len(completed.stderr),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
    }


def main() -> int:
    args = parser().parse_args()
    if min(args.observations, args.attempts, args.packages) < 1:
        raise SystemExit("counts must be positive")
    if args.attempts > args.observations:
        raise SystemExit("attempts must not exceed observations")
    verification_before = run_verification(args.verification_command)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = MetricsStore(root)
        store.initialize()
        source = store.register_source(name="synthetic-scale", source_type="benchmark")[
            "source"
        ]
        started = time.monotonic()
        for offset in range(0, args.observations, contracts.MAX_IMPORT_RECORDS):
            batch = []
            for index in range(
                offset,
                min(offset + contracts.MAX_IMPORT_RECORDS, args.observations),
            ):
                attempt = index % args.attempts
                package = index % args.packages
                batch.append(
                    contracts.make_observation(
                        source_id=source["source_id"],
                        record_type="execution_attempt",
                        observation_id=f"scale-{index}",
                        observed_at="2026-09-08T10:00:00Z",
                        data={
                            "execution_id": f"attempt-{attempt}",
                            "package_id": f"package-{package}",
                            "status": "completed",
                            "duration_seconds": 1,
                        },
                    )
                )
            store.ingest(batch)
        import_seconds = time.monotonic() - started
        report_started = time.monotonic()
        report = build_report(store, evaluation_time="2026-09-08T11:00:00Z")
        report_seconds = time.monotonic() - report_started
        metrics = {item["metric_id"]: item for item in report["metrics"]}
        reported_attempts = metrics["MET-002"]["coverage"]["total"]
        if reported_attempts != args.attempts:
            raise RuntimeError(
                "benchmark logical-attempt cardinality differs from --attempts"
            )
        result = {
            "schema_version": 1,
            "kind": "ORCHESTRATOR_METRICS_SCALE_RESULT",
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "logical_cpu_count": os.cpu_count(),
            "observations": args.observations,
            "attempts": args.attempts,
            "packages": args.packages,
            "import_seconds": import_seconds,
            "report_seconds": report_seconds,
            "archive_bytes": directory_size(store.root),
            "peak_memory_bytes": peak_memory_bytes(),
            "reported_observations": report["observation_count"],
            "reported_logical_attempts": reported_attempts,
            "universal_performance_claim": False,
            "verification_before": verification_before,
            **source_revision(Path(__file__).resolve().parent.parent),
        }
    result["verification_after"] = run_verification(args.verification_command)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
