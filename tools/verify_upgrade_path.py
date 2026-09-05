#!/usr/bin/env python3
"""Verify a released CLI state fixture with a newer installed CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

VERSION_PATTERN = re.compile(r"^orchestrator-engine ([0-9]+\.[0-9]+\.[0-9]+)$")
EVENT_ID = "upgrade-compatibility-event"
TASK_ID = "UPGRADE-COMPATIBILITY-001"
COMMAND_TIMEOUT_SECONDS = 30


class UpgradePathError(RuntimeError):
    """A CLI invocation or compatibility invariant failed."""


def run_cli(
    cli: Path,
    project: Path | None,
    *arguments: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> dict[str, Any]:
    command = [str(cli)]
    if project is not None:
        command.extend(["--project-root", str(project)])
    command.extend(arguments)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise UpgradePathError(
            f"CLI timed out for {arguments[0]} after {COMMAND_TIMEOUT_SECONDS}s"
        ) from error
    if completed.returncode not in allowed_returncodes:
        detail = (completed.stderr or completed.stdout).replace("\n", " ")[:500]
        raise UpgradePathError(
            f"CLI exited {completed.returncode} for {arguments[0]}: {detail}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise UpgradePathError(
            f"CLI returned invalid JSON for {arguments[0]}"
        ) from error
    if not isinstance(value, dict):
        raise UpgradePathError(f"CLI returned non-object JSON for {arguments[0]}")
    return value


def cli_version(cli: Path) -> str:
    try:
        completed = subprocess.run(
            [str(cli), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise UpgradePathError(
            f"CLI version check timed out after {COMMAND_TIMEOUT_SECONDS}s"
        ) from error
    match = VERSION_PATTERN.fullmatch(completed.stdout.strip())
    if completed.returncode != 0 or match is None:
        raise UpgradePathError(f"cannot read CLI version from {cli}")
    return match.group(1)


def state_digest(project: Path) -> tuple[str, int]:
    state = project / ".orchestrator"
    files = sorted(path for path in state.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(state).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest(), len(files)


def write_baseline_artifacts(project: Path) -> tuple[Path, Path]:
    artifact_root = project / ".orchestrator" / "upgrade-compatibility"
    artifact_root.mkdir(parents=True, exist_ok=True)
    result = artifact_root / "result.json"
    evidence = artifact_root / "evidence.json"
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "UPGRADE_COMPATIBILITY_RESULT",
                "task_id": TASK_ID,
                "status": "completed",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "UPGRADE_COMPATIBILITY_EVIDENCE",
                "task_id": TASK_ID,
                "fixture": "synthetic",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return result, evidence


def verify_upgrade_path(
    baseline_cli: Path,
    current_cli: Path,
    project: Path,
) -> dict[str, Any]:
    baseline_version = cli_version(baseline_cli)
    current_version = cli_version(current_cli)
    adoption = run_cli(baseline_cli, project, "adopt")
    if adoption.get("status") != "created":
        raise UpgradePathError("baseline adoption did not create a clean fixture")

    result, evidence = write_baseline_artifacts(project)
    emitted = run_cli(
        baseline_cli,
        project,
        "emit",
        "--task-id",
        TASK_ID,
        "--terminal-status",
        "completed",
        "--result",
        str(result),
        "--evidence",
        str(evidence),
        "--event-id",
        EVENT_ID,
    )
    if emitted.get("event", {}).get("event_id") != EVENT_ID:
        raise UpgradePathError("baseline CLI did not emit the expected event")

    before_digest, before_count = state_digest(project)
    upgrade = run_cli(
        current_cli,
        project,
        "upgrade",
        "check",
        allowed_returncodes=(0, 2),
    )
    after_digest, after_count = state_digest(project)
    if before_digest != after_digest or before_count != after_count:
        raise UpgradePathError("upgrade check modified baseline state")
    if upgrade.get("kind") != "ORCHESTRATOR_UPGRADE_CHECK":
        raise UpgradePathError("current CLI returned an unexpected upgrade report")
    if upgrade.get("status") == "blocked":
        raise UpgradePathError("current CLI blocked the baseline fixture")

    first_scan = run_cli(current_cli, project, "watcher", "--action", "record", "once")
    second_scan = run_cli(
        current_cli, project, "watcher", "--action", "record", "once"
    )
    if first_scan.get("new_count") != 1 or second_scan.get("new_count") != 0:
        raise UpgradePathError("current watcher did not consume baseline state once")
    event_path = project / ".orchestrator" / "events" / f"{EVENT_ID}.json"
    if not event_path.is_file() or not result.is_file() or not evidence.is_file():
        raise UpgradePathError("upgrade verification removed durable audit artifacts")

    return {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_UPGRADE_PATH_REPORT",
        "status": "passed",
        "baseline_version": baseline_version,
        "current_version": current_version,
        "upgrade_status": upgrade["status"],
        "read_only_state_digest": before_digest,
        "read_only_file_count": before_count,
        "first_scan_count": first_scan["new_count"],
        "second_scan_count": second_scan["new_count"],
        "durable_artifacts_preserved": True,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-cli", type=Path, required=True)
    parser.add_argument("--current-cli", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--keep-fixture", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    fixture = (
        args.fixture_root.expanduser().resolve()
        if args.fixture_root is not None
        else Path(tempfile.mkdtemp(prefix="orchestrator-upgrade-"))
    )
    created_temporary = args.fixture_root is None
    if not created_temporary:
        if fixture.exists():
            print(f"ERROR: fixture root already exists: {fixture}", file=sys.stderr)
            return 1
        fixture.mkdir(parents=True)
    try:
        report = verify_upgrade_path(
            args.baseline_cli.expanduser().resolve(),
            args.current_cli.expanduser().resolve(),
            fixture,
        )
    except (OSError, UpgradePathError) as error:
        print(f"ERROR: {error}; fixture retained at {fixture}", file=sys.stderr)
        return 1
    if not args.keep_fixture:
        shutil.rmtree(fixture)
    report["fixture_status"] = "retained" if args.keep_fixture else "removed"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
