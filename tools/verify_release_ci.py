#!/usr/bin/env python3
"""Verify that an exact release commit has a successful GitHub Actions run."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

MAX_INPUT_BYTES = 512 * 1024
SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9_.-](?:[A-Za-z0-9_.-]{0,99})$"
)


class ReleaseCIError(RuntimeError):
    """The supplied run list does not prove a successful release commit."""


def repository_url_matches(url: object, *, repository: str, hostname: str) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    expected_path = f"/{repository}/actions/runs/".casefold()
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == hostname.casefold()
        and parsed.path.casefold().startswith(expected_path)
    )


def load_runs(path: Path) -> list[dict[str, object]]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ReleaseCIError(f"cannot read run list: {error}") from error
    if len(payload) > MAX_INPUT_BYTES:
        raise ReleaseCIError("run list exceeds the bounded input limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseCIError("run list is not valid UTF-8 JSON") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ReleaseCIError("run list must be a JSON array of objects")
    return value


def verify_release_ci(
    runs: list[dict[str, object]],
    *,
    expected_sha: str,
    repository: str,
    hostname: str,
    workflow_name: str,
    branch: str,
) -> dict[str, object]:
    candidates: dict[int, dict[str, object]] = {}
    for run in runs:
        head_sha = run.get("headSha")
        if not isinstance(head_sha, str) or head_sha.lower() != expected_sha:
            continue
        if run.get("workflowName") != workflow_name:
            continue
        if run.get("event") != "push" or run.get("headBranch") != branch:
            continue
        if not repository_url_matches(
            run.get("url"), repository=repository, hostname=hostname
        ):
            raise ReleaseCIError("matching run has an unexpected repository URL")
        run_id = run.get("databaseId")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise ReleaseCIError("matching run has an invalid database ID")
        candidates[run_id] = run
    if not candidates:
        raise ReleaseCIError(
            "no matching push CI run exists for the exact release commit"
        )
    run_id = max(candidates)
    selected = candidates[run_id]
    status = selected.get("status")
    conclusion = selected.get("conclusion")
    if status != "completed" or conclusion != "success":
        raise ReleaseCIError(
            "latest matching CI run is not completed successfully: "
            f"status={status!r} conclusion={conclusion!r}"
        )
    return {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_RELEASE_CI_VERIFICATION",
        "repository": repository,
        "hostname": hostname,
        "workflow_name": workflow_name,
        "branch": branch,
        "head_sha": expected_sha,
        "run_id": run_id,
        "status": status,
        "conclusion": conclusion,
        "url": selected.get("url"),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--hostname", default="github.com")
    parser.add_argument("--workflow-name", default="CI")
    parser.add_argument("--branch", default="main")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    expected_sha = args.expected_sha.strip().lower()
    if not SHA_PATTERN.fullmatch(expected_sha):
        print(
            "ERROR: expected-sha must be a full hexadecimal commit ID",
            file=sys.stderr,
        )
        return 1
    if not REPOSITORY_PATTERN.fullmatch(args.repository):
        print("ERROR: repository must use OWNER/REPO form", file=sys.stderr)
        return 1
    try:
        report = verify_release_ci(
            load_runs(args.input),
            expected_sha=expected_sha,
            repository=args.repository,
            hostname=args.hostname,
            workflow_name=args.workflow_name,
            branch=args.branch,
        )
    except ReleaseCIError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
