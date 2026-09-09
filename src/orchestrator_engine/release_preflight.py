"""Read-only release readiness checks for an OrchestratorEngine checkout."""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from . import core, diagnostics

KIND = "ORCHESTRATOR_RELEASE_PREFLIGHT"
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:rc[1-9][0-9]*)?$")
REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MAX_COMMAND_OUTPUT = 2_000
MAX_UNTRACKED_SCAN_BYTES = 2 * 1024 * 1024


class ReleasePreflightError(RuntimeError):
    """The release preflight could not inspect the checkout."""


def _run_git(
    root: Path,
    *args: str,
    timeout_seconds: float = 20.0,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise ReleasePreflightError(f"git {args[0]} timed out") from error


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def _git_output(root: Path, *args: str) -> str:
    completed = _run_git(root, *args)
    if completed.returncode != 0:
        detail = _decode(completed.stderr or completed.stdout)[:MAX_COMMAND_OUTPUT]
        raise ReleasePreflightError(f"git {args[0]} failed: {detail or 'no output'}")
    return _decode(completed.stdout)


def resolve_git_commit(root: Path, reference: str) -> str:
    """Resolve one local Git reference to an immutable full commit ID."""

    normalized = reference.strip()
    if not normalized or len(normalized) > 255:
        raise ReleasePreflightError("Git reference must contain 1 to 255 characters")
    if any(character in normalized for character in ("\x00", "\n", "\r")):
        raise ReleasePreflightError("Git reference contains invalid characters")
    commit = _git_output(
        root.resolve(),
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{normalized}^{{commit}}",
    ).casefold()
    if not SHA_PATTERN.fullmatch(commit):
        raise ReleasePreflightError(
            "Git reference did not resolve to one full commit ID"
        )
    return commit


def _literal_source_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise ReleasePreflightError(f"{path} has no literal __version__ assignment")


def _release_versions(root: Path) -> tuple[str, dict[str, str]]:
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ReleasePreflightError(
            "pyproject.toml project.version must be x.y.z or x.y.zrcN"
        )
    with (root / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    lock_version = None
    for package in lock.get("package", []):
        if isinstance(package, dict) and package.get("name") == "orchestrator-engine":
            lock_version = package.get("version")
            break
    if not isinstance(lock_version, str):
        raise ReleasePreflightError("uv.lock has no orchestrator-engine package")
    return version, {
        "pyproject.toml": version,
        "src/orchestrator_engine/__init__.py": _literal_source_version(
            root / "src" / "orchestrator_engine" / "__init__.py"
        ),
        "uv.lock": lock_version,
    }


def _metadata_errors(root: Path, version: str, versions: dict[str, str]) -> list[str]:
    errors = [
        f"{path} has version {found}; expected {version}"
        for path, found in versions.items()
        if found != version
    ]
    markers = {
        "CHANGELOG.md": [f"## [{version}] -"],
        "README.md": [f"OrchestratorEngine.git@v{version}"],
        "docs/setup-guide.md": [f"OrchestratorEngine.git@v{version}"],
        "docs/upgrade-guide.md": [f"OrchestratorEngine.git@v{version}"],
    }
    release_label = "The current release is"
    if "rc" in version:
        release_label = "The current release candidate is"
    markers["docs/upgrade-guide.md"].append(f"{release_label} `{version}`")
    for relative, expected_markers in markers.items():
        try:
            content = (root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f"cannot read {relative}: {error}")
            continue
        for marker in expected_markers:
            if marker not in content:
                errors.append(f"{relative} is missing release marker for {version}")
    return errors


def _untracked_paths(root: Path) -> list[Path]:
    payload = _run_git(
        root, "ls-files", "--others", "--exclude-standard", "-z"
    )
    if payload.returncode != 0:
        raise ReleasePreflightError(
            f"git ls-files failed: {_decode(payload.stderr)[:MAX_COMMAND_OUTPUT]}"
        )
    return [root / os.fsdecode(item) for item in payload.stdout.split(b"\x00") if item]


def _untracked_whitespace_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _untracked_paths(root):
        try:
            if path.is_symlink() or path.stat().st_size > MAX_UNTRACKED_SCAN_BYTES:
                continue
            data = path.read_bytes()
        except OSError as error:
            errors.append(
                f"cannot inspect untracked file {path.relative_to(root)}: {error}"
            )
            continue
        if b"\x00" in data:
            continue
        for line_number, line in enumerate(data.splitlines(), start=1):
            if line.endswith((b" ", b"\t")):
                errors.append(
                    f"{path.relative_to(root)}:{line_number}: trailing whitespace"
                )
    return errors


def _command_path(command: str) -> str | None:
    if "/" in command or "\\" in command:
        path = Path(command).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        return None
    return shutil.which(command)


def _wsl_gh_suggestion() -> str | None:
    uname = getattr(os, "uname", None)
    release = uname().release if uname is not None else ""
    if not (
        os.environ.get("WSL_DISTRO_NAME")
        or "microsoft" in release.casefold()
    ):
        return None
    candidates = (
        shutil.which("gh.exe"),
        "/mnt/c/Program Files/GitHub CLI/gh.exe",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def _check(name: str, status: str, reason: str, **details: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "reason": reason, **details}


def _completion_delivery(
    project: Path,
    *,
    state_dir: str,
    host: str | None,
    require_watcher: bool,
) -> dict[str, Any]:
    """Report the bound host's delivery channel using the doctor check.

    Every supported host proves its own channel locally: a callback host
    through its running service, a stream host through a fresh armed stream.
    Reusing one channel check keeps release readiness host-symmetric.
    """

    try:
        channel = diagnostics.check_watcher_channel(
            project, state_dir=state_dir, host=host
        )
    except (OSError, RuntimeError, ValueError) as error:
        return _check(
            "completion_delivery",
            "fail" if require_watcher else "warning",
            f"completion delivery channel could not be inspected: {error}",
            host=host,
        )
    data = channel.get("data")
    data = data if isinstance(data, dict) else {}
    service = data.get("service_status")
    service = service if isinstance(service, dict) else None
    stream = data.get("stream_status")
    stream = stream if isinstance(stream, dict) else None
    delivery = service or stream
    return _check(
        "completion_delivery",
        "pass"
        if channel.get("status") == "ok"
        else ("fail" if require_watcher else "warning"),
        str(channel.get("detail")),
        host=data.get("host") or host,
        service_status=service.get("status") if service else None,
        stream_status=stream.get("status") if stream else None,
        pending_inbox_count=delivery.get("pending_inbox_count") if delivery else None,
    )


def run_preflight(
    root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    remote: str = "origin",
    branch: str = "main",
    host: str | None = None,
    gh_command: str = "gh",
    offline: bool = False,
    require_watcher: bool = False,
) -> dict[str, Any]:
    """Inspect release readiness without modifying Git or runtime state."""

    project = root.expanduser().resolve()
    if not REMOTE_PATTERN.fullmatch(remote):
        raise ReleasePreflightError("remote must be a simple Git remote name")
    branch_check = _run_git(project, "check-ref-format", "--branch", branch)
    if branch_check.returncode != 0:
        raise ReleasePreflightError("branch is not a valid Git branch name")
    version, versions = _release_versions(project)
    tag = f"v{version}"
    head_sha = resolve_git_commit(project, "HEAD")
    checks: list[dict[str, Any]] = []

    metadata_errors = _metadata_errors(project, version, versions)
    checks.append(
        _check(
            "release_metadata",
            "pass" if not metadata_errors else "fail",
            (
                "release metadata is consistent"
                if not metadata_errors
                else metadata_errors[0]
            ),
            errors=metadata_errors,
        )
    )

    status_payload = _run_git(project, "status", "--porcelain=v1", "-z")
    if status_payload.returncode != 0:
        raise ReleasePreflightError("git status failed")
    dirty_entries = [item for item in status_payload.stdout.split(b"\x00") if item]
    checks.append(
        _check(
            "clean_worktree",
            "pass" if not dirty_entries else "fail",
            (
                "worktree is clean"
                if not dirty_entries
                else f"worktree has {len(dirty_entries)} changed path(s)"
            ),
            changed_path_count=len(dirty_entries),
        )
    )

    tracked_whitespace = _run_git(project, "diff", "--check", "HEAD", "--")
    tracked_errors = [
        line for line in _decode(tracked_whitespace.stdout).splitlines() if line
    ]
    untracked_errors = _untracked_whitespace_errors(project)
    whitespace_errors = tracked_errors + untracked_errors
    checks.append(
        _check(
            "whitespace",
            "pass" if not whitespace_errors else "fail",
            "tracked and untracked text files have no whitespace errors"
            if not whitespace_errors
            else whitespace_errors[0],
            errors=whitespace_errors[:20],
            truncated=len(whitespace_errors) > 20,
        )
    )

    remote_ref = f"refs/remotes/{remote}/{branch}"
    remote_sha = resolve_git_commit(project, remote_ref)
    checks.append(
        _check(
            "remote_head",
            "pass" if remote_sha == head_sha else "fail",
            f"HEAD matches {remote}/{branch}"
            if remote_sha == head_sha
            else f"HEAD {head_sha} does not match {remote}/{branch} {remote_sha}",
            head_sha=head_sha,
            remote_sha=remote_sha,
        )
    )

    local_tag = _run_git(project, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}")
    if local_tag.returncode == 1:
        local_tag_status = "pass"
        local_tag_reason = f"local tag {tag} is absent"
    elif local_tag.returncode == 0:
        local_tag_status = "fail"
        local_tag_reason = f"local tag {tag} already exists"
    else:
        local_tag_status = "fail"
        local_tag_reason = (
            f"local tag check failed with exit code {local_tag.returncode}"
        )
    checks.append(
        _check(
            "local_tag_absent",
            local_tag_status,
            local_tag_reason,
        )
    )
    if offline:
        checks.append(
            _check(
                "remote_tag_absent",
                "warning",
                "remote tag check skipped by --offline",
            )
        )
    else:
        remote_tag = _run_git(
            project,
            "ls-remote",
            "--exit-code",
            "--tags",
            remote,
            f"refs/tags/{tag}",
        )
        remote_tag_absent = remote_tag.returncode == 2
        checks.append(
            _check(
                "remote_tag_absent",
                "pass" if remote_tag_absent else "fail",
                f"remote tag {tag} is absent"
                if remote_tag_absent
                else (
                    f"remote tag {tag} already exists"
                    if remote_tag.returncode == 0
                    else (
                        "remote tag check failed: "
                        + _decode(remote_tag.stderr)[:MAX_COMMAND_OUTPUT]
                    )
                ),
            )
        )

    resolved_gh = _command_path(gh_command)
    gh_suggestion = None if resolved_gh else _wsl_gh_suggestion()
    checks.append(
        _check(
            "github_cli",
            "pass" if resolved_gh else "warning",
            f"GitHub CLI resolved to {resolved_gh}"
            if resolved_gh
            else (
                "GitHub CLI is not on PATH; configure gh_command to the "
                "detected WSL bridge"
                if gh_suggestion
                else "GitHub CLI is not available; ci watch and pr watch require it"
            ),
            command=gh_command,
            resolved_path=resolved_gh,
            suggested_command=gh_suggestion,
        )
    )

    checks.append(
        _completion_delivery(
            project,
            state_dir=state_dir,
            host=host,
            require_watcher=require_watcher,
        )
    )

    failed = sum(item["status"] == "fail" for item in checks)
    warnings = sum(item["status"] == "warning" for item in checks)
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": KIND,
        "status": "ready" if failed == 0 else "blocked",
        "version": version,
        "tag": tag,
        "head_sha": head_sha,
        "remote": remote,
        "branch": branch,
        "failed_check_count": failed,
        "warning_count": warnings,
        "checks": checks,
        "ci_watch": {
            "expected_head_sha": head_sha,
            "expected_head_from_git": "HEAD",
            "gh_command": resolved_gh or gh_suggestion or gh_command,
        },
        "checked_at": core.utc_now(),
    }


def exit_code(report: dict[str, Any]) -> int:
    return 0 if report.get("status") == "ready" else 1
