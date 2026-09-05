#!/usr/bin/env python3
"""Validate immutable release-tag provenance against an exact checkout SHA."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

RELEASE_VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:rc[1-9][0-9]*)?$"
)
SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
INTERNAL_TAG_REF = "refs/orchestrator/release-tag"
INTERNAL_MAIN_REF = "refs/orchestrator/release-main"
MAX_ERROR_CHARS = 2_000


class ReleaseSourceError(RuntimeError):
    """The fetched tag does not prove the requested release source."""


def _bounded_error(completed: subprocess.CompletedProcess[str]) -> str:
    message = (completed.stderr or completed.stdout or "no command output").strip()
    if len(message) > MAX_ERROR_CHARS:
        message = message[:MAX_ERROR_CHARS] + "..."
    return message


def _git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise ReleaseSourceError(
            f"git {args[0]} failed: {_bounded_error(completed)}"
        )
    return completed


def _git_output(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.strip()


def validate_release_source(
    *,
    root: Path,
    tag: str,
    version: str,
    expected_sha: str,
    remote: str = "origin",
    main_branch: str = "main",
) -> dict[str, object]:
    root = root.resolve()
    expected_sha = expected_sha.strip().lower()
    if not RELEASE_VERSION_PATTERN.fullmatch(version):
        raise ReleaseSourceError("version must use x.y.z or x.y.zrcN form")
    if tag != f"v{version}":
        raise ReleaseSourceError(
            f"release tag {tag!r} does not match version {version!r}"
        )
    if not SHA_PATTERN.fullmatch(expected_sha):
        raise ReleaseSourceError("expected-sha must be a full hexadecimal commit ID")
    if not REMOTE_PATTERN.fullmatch(remote):
        raise ReleaseSourceError("remote must be a simple Git remote name")
    if _git(root, "check-ref-format", "--branch", main_branch, check=False).returncode:
        raise ReleaseSourceError("main-branch is not a valid Git branch name")

    tag_ref = f"refs/tags/{tag}"
    _git(root, "update-ref", "-d", INTERNAL_TAG_REF, check=False)
    _git(root, "update-ref", "-d", INTERNAL_MAIN_REF, check=False)
    try:
        _git(
            root,
            "fetch",
            "--force",
            "--no-tags",
            remote,
            f"{tag_ref}:{INTERNAL_TAG_REF}",
            f"refs/heads/{main_branch}:{INTERNAL_MAIN_REF}",
        )
        object_type = _git_output(root, "cat-file", "-t", INTERNAL_TAG_REF)
        if object_type != "tag":
            raise ReleaseSourceError(
                f"release tag {tag!r} must be annotated; found {object_type!r}"
            )
        commit_sha = _git_output(root, "rev-list", "-n", "1", INTERNAL_TAG_REF)
        if commit_sha.lower() != expected_sha:
            raise ReleaseSourceError(
                f"release tag targets {commit_sha}, but expected {expected_sha}"
            )
        head_sha = _git_output(root, "rev-parse", "HEAD")
        if head_sha.lower() != expected_sha:
            raise ReleaseSourceError(
                f"checkout HEAD is {head_sha}, but expected {expected_sha}"
            )
        ancestry = _git(
            root,
            "merge-base",
            "--is-ancestor",
            commit_sha,
            INTERNAL_MAIN_REF,
            check=False,
        )
        if ancestry.returncode != 0:
            branch_label = f"{remote}/{main_branch}"
            raise ReleaseSourceError(
                f"release commit {commit_sha} is not contained in {branch_label}"
            )
        source_date_epoch_text = _git_output(
            root, "show", "-s", "--format=%ct", commit_sha
        )
        try:
            source_date_epoch = int(source_date_epoch_text)
        except ValueError as error:
            raise ReleaseSourceError(
                "release commit has an invalid timestamp"
            ) from error
        if source_date_epoch < 0:
            raise ReleaseSourceError("release commit has an invalid timestamp")
        return {
            "schema_version": 1,
            "kind": "ORCHESTRATOR_RELEASE_SOURCE_VERIFICATION",
            "version": version,
            "tag": tag,
            "prerelease": "rc" in version,
            "commit_sha": commit_sha.lower(),
            "remote": remote,
            "main_branch": main_branch,
            "source_date_epoch": source_date_epoch,
            "annotated_tag_verified": True,
            "exact_checkout_verified": True,
            "main_ancestry_verified": True,
        }
    finally:
        _git(root, "update-ref", "-d", INTERNAL_TAG_REF, check=False)
        _git(root, "update-ref", "-d", INTERNAL_MAIN_REF, check=False)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--main-branch", default="main")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = validate_release_source(
            root=args.root,
            tag=args.tag,
            version=args.version,
            expected_sha=args.expected_sha,
            remote=args.remote,
            main_branch=args.main_branch,
        )
    except (OSError, ReleaseSourceError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
