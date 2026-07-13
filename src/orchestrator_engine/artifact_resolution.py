"""Hash-bound operator resolutions for malformed durable artifact metadata."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from . import core

ARTIFACT_RESOLUTION_KIND = "ORCHESTRATOR_ARTIFACT_RESOLUTION"
DIAGNOSTIC_CODE = "schema_metadata_malformed"
MAX_PATH_LENGTH = 4096
MAX_REASON_LENGTH = 4096
MAX_OBSERVED_VALUE_LENGTH = 128


class ArtifactResolutionError(RuntimeError):
    """A deterministic artifact resolution failure."""


def resolutions_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "artifact-resolutions"


def artifact_identity(relative_path: str, artifact_sha256: str) -> str:
    identity = f"{relative_path}\0{artifact_sha256}".encode()
    return hashlib.sha256(identity).hexdigest()


def resolution_path_for(
    project_root: Path,
    artifact_relative_path: str,
    artifact_sha256: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return resolutions_root(project_root, state_dir=state_dir) / (
        f"{artifact_identity(artifact_relative_path, artifact_sha256)}.json"
    )


def resolve_artifact_path(
    project_root: Path,
    artifact_path: str | Path,
    *,
    state_dir: str,
) -> tuple[Path, str]:
    project = project_root.expanduser().resolve()
    state = core.state_root(project, state_dir=state_dir).resolve()
    supplied = Path(artifact_path).expanduser()
    candidates = [supplied] if supplied.is_absolute() else [project / supplied]
    if not supplied.is_absolute():
        candidates.append(state / supplied)
    resolved: Path | None = None
    relative: Path | None = None
    for candidate in candidates:
        if candidate.is_symlink():
            raise ArtifactResolutionError(
                "artifact resolution does not accept symlinks"
            )
        candidate_resolved = candidate.resolve()
        try:
            candidate_relative = candidate_resolved.relative_to(state)
        except ValueError:
            continue
        resolved = candidate_resolved
        relative = candidate_relative
        break
    if resolved is None or relative is None:
        raise ArtifactResolutionError(
            f"artifact must be inside orchestrator state root: {state}"
        )
    if relative.parts and relative.parts[0] == "artifact-resolutions":
        raise ArtifactResolutionError(
            "artifact resolution records cannot resolve other resolution records"
        )
    if len(relative.as_posix()) > MAX_PATH_LENGTH:
        raise ArtifactResolutionError(
            f"artifact path exceeds {MAX_PATH_LENGTH} characters"
        )
    if not resolved.is_file():
        raise ArtifactResolutionError(f"artifact is not a regular file: {resolved}")
    return resolved, relative.as_posix()


def _open_regular_file(path: Path) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as error:
        raise ArtifactResolutionError(
            f"cannot inspect artifact: {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise ArtifactResolutionError(f"artifact must not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArtifactResolutionError(
            f"cannot open artifact safely: {path}: {error}"
        ) from error
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or (
        (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(descriptor)
        raise ArtifactResolutionError(f"artifact changed before safe open: {path}")
    return descriptor, opened


def _verify_open_file_identity(path: Path, opened: os.stat_result) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise ArtifactResolutionError(
            f"artifact changed while reading: {path}"
        ) from error
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if stat.S_ISLNK(current.st_mode) or any(
        getattr(current, field) != getattr(opened, field) for field in stable_fields
    ):
        raise ArtifactResolutionError(f"artifact changed while reading: {path}")


def _verify_descriptor_unchanged(
    descriptor: int,
    opened: os.stat_result,
    path: Path,
) -> None:
    current = os.fstat(descriptor)
    stable_fields = ("st_size", "st_mtime_ns", "st_ctime_ns")
    if any(
        getattr(current, field) != getattr(opened, field) for field in stable_fields
    ):
        raise ArtifactResolutionError(f"artifact changed while reading: {path}")


def _read_regular_json_and_hash(path: Path) -> tuple[dict[str, Any], str]:
    descriptor, opened = _open_regular_file(path)
    with os.fdopen(descriptor, "rb") as handle:
        content = handle.read()
        _verify_descriptor_unchanged(handle.fileno(), opened, path)
    _verify_open_file_identity(path, opened)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactResolutionError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise ArtifactResolutionError(f"JSON artifact must be an object: {path}")
    return value, hashlib.sha256(content).hexdigest()


def _sha256_regular_file(path: Path) -> str:
    descriptor, opened = _open_regular_file(path)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        _verify_descriptor_unchanged(handle.fileno(), opened, path)
    _verify_open_file_identity(path, opened)
    return digest.hexdigest()


def write_resolution(
    project_root: Path,
    *,
    artifact_path: str | Path,
    reason: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason.strip()) > MAX_REASON_LENGTH
    ):
        raise ArtifactResolutionError(
            f"reason must contain at most {MAX_REASON_LENGTH} characters"
        )
    resolved, relative = resolve_artifact_path(
        project,
        artifact_path,
        state_dir=state_dir,
    )
    survey = core.survey_schema_versions(project, state_dir=state_dir)
    surveyed_finding = next(
        (
            item
            for item in survey["unsupported"]
            if Path(str(item.get("path"))).resolve() == resolved
        ),
        None,
    )
    if surveyed_finding is None:
        raise ArtifactResolutionError(
            "artifact is not currently reported with malformed schema metadata"
        )

    observed, artifact_sha256 = _read_regular_json_and_hash(resolved)
    if type(observed.get("schema_version")) is int:
        raise ArtifactResolutionError(
            "artifact is not currently reported with malformed schema metadata"
        )
    path = resolution_path_for(
        project,
        relative,
        artifact_sha256,
        state_dir=state_dir,
    )
    if path.exists():
        existing = load_resolution(path)
        if existing["reason"] != reason.strip():
            raise ArtifactResolutionError(
                "artifact resolution already exists for these bytes with a different "
                "reason; immutable resolution records cannot be replaced"
            )
        return {**existing, "resolution_path": str(path), "idempotent": True}
    observed_kind = observed.get("kind")
    if (
        not isinstance(observed_kind, str)
        or len(observed_kind) > MAX_OBSERVED_VALUE_LENGTH
    ):
        observed_kind = None
    observed_version = bounded_observed_value(observed.get("schema_version"))
    resolution = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": ARTIFACT_RESOLUTION_KIND,
        "artifact_path": relative,
        "artifact_sha256": artifact_sha256,
        "diagnostic_code": DIAGNOSTIC_CODE,
        "observed_kind": observed_kind,
        "observed_schema_version": observed_version,
        "reason": reason.strip(),
        "created_at": core.utc_now(),
    }
    if not core.claim_json(path, resolution):
        existing = load_resolution(path)
        if existing["reason"] != reason.strip():
            raise ArtifactResolutionError(
                "artifact resolution already exists for these bytes with a different "
                "reason; immutable resolution records cannot be replaced"
            )
        return {**existing, "resolution_path": str(path), "idempotent": True}
    return {**resolution, "resolution_path": str(path), "idempotent": False}


def load_resolution(path: Path) -> dict[str, Any]:
    resolution, _ = _read_regular_json_and_hash(path)
    validate_resolution(resolution, path=path)
    return resolution


def bounded_observed_value(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str) and len(value) <= MAX_OBSERVED_VALUE_LENGTH:
        return value
    return type(value).__name__


def validate_resolution(resolution: dict[str, Any], *, path: Path) -> None:
    if not core.is_supported_schema_version(resolution.get("schema_version")):
        raise ArtifactResolutionError(f"unsupported artifact resolution schema: {path}")
    if resolution.get("kind") != ARTIFACT_RESOLUTION_KIND:
        raise ArtifactResolutionError(f"unsupported artifact resolution kind: {path}")
    artifact_path = resolution.get("artifact_path")
    if (
        not isinstance(artifact_path, str)
        or not artifact_path
        or len(artifact_path) > MAX_PATH_LENGTH
        or Path(artifact_path).is_absolute()
        or ".." in Path(artifact_path).parts
    ):
        raise ArtifactResolutionError(f"invalid resolved artifact path: {path}")
    digest = resolution.get("artifact_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ArtifactResolutionError(f"invalid resolved artifact hash: {path}")
    if resolution.get("diagnostic_code") != DIAGNOSTIC_CODE:
        raise ArtifactResolutionError(f"invalid artifact diagnostic code: {path}")
    expected_name = f"{artifact_identity(artifact_path, digest)}.json"
    if path.name != expected_name:
        raise ArtifactResolutionError(
            f"artifact resolution filename does not match its identity: {path}"
        )
    reason = resolution.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > MAX_REASON_LENGTH
    ):
        raise ArtifactResolutionError(f"invalid artifact resolution reason: {path}")
    observed_kind = resolution.get("observed_kind")
    if observed_kind is not None and (
        not isinstance(observed_kind, str)
        or len(observed_kind) > MAX_OBSERVED_VALUE_LENGTH
    ):
        raise ArtifactResolutionError(f"invalid observed artifact kind: {path}")
    observed_version = resolution.get("observed_schema_version")
    if not (
        observed_version is None
        or isinstance(observed_version, bool | int | float)
        or (
            isinstance(observed_version, str)
            and len(observed_version) <= MAX_OBSERVED_VALUE_LENGTH
        )
    ):
        raise ArtifactResolutionError(f"invalid observed schema version: {path}")
    if not isinstance(resolution.get("created_at"), str):
        raise ArtifactResolutionError(f"invalid artifact resolution timestamp: {path}")


def list_resolutions(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    state = core.state_root(project, state_dir=state_dir).resolve()
    items: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    root = resolutions_root(project, state_dir=state_dir)
    for path in sorted(root.glob("*.json")):
        try:
            resolution = load_resolution(path)
        except (OSError, core.OrchestratorError, ArtifactResolutionError) as error:
            invalid.append({"path": str(path), "error": str(error)})
            continue
        artifact = state / resolution["artifact_path"]
        artifact_error = None
        try:
            current_hash = _sha256_regular_file(artifact)
        except ArtifactResolutionError as error:
            current_hash = None
            artifact_error = str(error)
        item = {
            **resolution,
            "resolution_path": str(path),
            "active": current_hash == resolution["artifact_sha256"],
            "current_sha256": current_hash,
        }
        if artifact_error is not None:
            item["artifact_error"] = artifact_error
        items.append(item)
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_ARTIFACT_RESOLUTION_LIST",
        "resolution_count": len(items),
        "invalid_count": len(invalid),
        "resolutions": items,
        "invalid": invalid,
        "generated_at": core.utc_now(),
    }


def partition_malformed(
    project_root: Path,
    malformed: list[dict[str, Any]],
    *,
    state_dir: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    project = project_root.expanduser().resolve()
    state = core.state_root(project, state_dir=state_dir).resolve()
    active: dict[tuple[str, str], dict[str, Any]] = {}
    listed = list_resolutions(project, state_dir=state_dir)
    for resolution in listed["resolutions"]:
        if not resolution.get("active"):
            continue
        artifact = (state / resolution["artifact_path"]).resolve()
        active[(str(artifact), resolution["artifact_sha256"])] = resolution

    unresolved: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for finding in malformed:
        path = Path(str(finding.get("path")))
        try:
            digest = _sha256_regular_file(path)
        except ArtifactResolutionError:
            unresolved.append(finding)
            continue
        resolution = active.get((str(path.resolve()), digest))
        if resolution is None:
            unresolved.append(finding)
        else:
            resolved.append({**finding, "resolution": resolution})
    return unresolved, resolved, listed["invalid"]
