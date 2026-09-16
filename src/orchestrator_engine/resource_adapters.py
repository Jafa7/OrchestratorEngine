"""Closed typed adapters for resource-managed verification operations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from . import core
from .resource_queue import ResourceError, digest

RELEASE_UPGRADE_KIND = "release-upgrade"
RELEASE_UPGRADE_INPUT_KIND = "ORCHESTRATOR_RELEASE_UPGRADE_INPUT"
RELEASE_UPGRADE_RESULT_KIND = "ORCHESTRATOR_RELEASE_UPGRADE_RESULT"
RELEASE_UPGRADE_PHASES = (
    "stack_start",
    "reset_previous",
    "fixture_load",
    "pre_assertions",
    "migrate_candidate",
    "post_assertions",
    "type_readback",
    "check_readback",
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_BOUNDARY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z", re.ASCII)
_RECIPE_FIELDS = {
    "kind",
    "inputs",
    "needs",
    "adapter",
    "previous_boundary",
    "fixtures",
    "pre_assertions",
    "post_assertions",
    "readbacks",
    "timeout_seconds",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant: {value}")


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ResourceError(f"{field} must be a nonempty bounded relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or ".orchestrator" in path.parts
    ):
        raise ResourceError(f"{field} must stay inside captured project inputs")
    return value


def _path_list(value: Any, field: str, *, maximum: int = 32) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > maximum
        or not all(isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ResourceError(f"{field} must be a nonempty unique bounded path list")
    return [_relative_path(item, field) for item in value]


def _covered(relative: str, inputs: list[str]) -> bool:
    path = PurePosixPath(relative)
    return any(
        path == PurePosixPath(root) or PurePosixPath(root) in path.parents
        for root in inputs
    )


def normalize_release_upgrade_recipe(recipe: Any) -> dict[str, Any]:
    """Validate the public typed recipe without accepting arbitrary commands."""

    if not isinstance(recipe, dict) or set(recipe) - _RECIPE_FIELDS:
        raise ResourceError("unsupported release-upgrade recipe fields")
    if recipe.get("kind") != RELEASE_UPGRADE_KIND:
        raise ResourceError("unsupported typed resource recipe kind")
    inputs = _path_list(recipe.get("inputs"), "inputs", maximum=64)
    needs = recipe.get("needs")
    if not isinstance(needs, list) or len(needs) != 1:
        raise ResourceError(
            "release-upgrade requires exactly one exclusive resource selector"
        )
    normalized_needs = []
    for need in needs:
        if (
            not isinstance(need, dict)
            or set(need) != {"resource", "mode"}
            or not isinstance(need.get("resource"), str)
            or not need["resource"]
            or len(need["resource"]) > 160
            or need.get("mode") != "exclusive"
        ):
            raise ResourceError(
                "release-upgrade resource needs must be explicit exclusive selectors"
            )
        normalized_needs.append(dict(need))
    adapter = _relative_path(recipe.get("adapter"), "adapter")
    previous = recipe.get("previous_boundary")
    if not isinstance(previous, str) or not _BOUNDARY.fullmatch(previous):
        raise ResourceError("previous boundary must be a bounded ASCII identifier")
    fixtures = _path_list(recipe.get("fixtures"), "fixtures", maximum=16)
    pre = _path_list(recipe.get("pre_assertions"), "pre_assertions")
    post = _path_list(recipe.get("post_assertions"), "post_assertions")
    readbacks = _path_list(recipe.get("readbacks"), "readbacks")
    timeout = recipe.get("timeout_seconds")
    if timeout is not None and (
        type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ResourceError("release-upgrade timeout must be positive and finite")
    for relative in [adapter, *fixtures, *pre, *post, *readbacks]:
        if not _covered(relative, inputs):
            raise ResourceError(
                f"release-upgrade path is not covered by recipe inputs: {relative}"
            )
    return {
        "kind": RELEASE_UPGRADE_KIND,
        "inputs": inputs,
        "needs": normalized_needs,
        "adapter": adapter,
        "previous_boundary": previous,
        "fixtures": fixtures,
        "pre_assertions": pre,
        "post_assertions": post,
        "readbacks": readbacks,
        **({"timeout_seconds": float(timeout)} if timeout is not None else {}),
    }


def compile_recipe(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    """Compile one registered recipe into private runner stages."""

    if recipe.get("kind") != RELEASE_UPGRADE_KIND:
        return recipe["stages"]
    value = normalize_release_upgrade_recipe(recipe)
    command = {
        "kind": RELEASE_UPGRADE_KIND,
        "adapter": value["adapter"],
        "previous_boundary": value["previous_boundary"],
        "fixtures": value["fixtures"],
        "pre_assertions": value["pre_assertions"],
        "post_assertions": value["post_assertions"],
        "readbacks": value["readbacks"],
    }
    if "timeout_seconds" in value:
        command["timeout_seconds"] = value["timeout_seconds"]
    return [
        {
            "id": RELEASE_UPGRADE_KIND,
            "needs": value["needs"],
            "commands": [command],
        }
    ]


def validate_compiled_command(command: Any) -> None:
    expected = {
        "kind",
        "adapter",
        "previous_boundary",
        "fixtures",
        "pre_assertions",
        "post_assertions",
        "readbacks",
        "timeout_seconds",
    }
    required = expected - {"timeout_seconds"}
    if (
        not isinstance(command, dict)
        or set(command) - expected
        or required - set(command)
    ):
        raise ResourceError("unsupported compiled release-upgrade command fields")
    normalize_release_upgrade_recipe(
        {
            **command,
            "inputs": [
                command["adapter"],
                *command["fixtures"],
                *command["pre_assertions"],
                *command["post_assertions"],
                *command["readbacks"],
            ],
            "needs": [{"resource": "compiled", "mode": "exclusive"}],
        }
    )


def _captured(root: Path, relative: str, manifest: dict[str, str]) -> dict[str, Any]:
    candidate = root / relative
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ResourceError(
            f"captured release-upgrade path is unavailable: {relative}"
        ) from error
    path = candidate.resolve()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not path.is_relative_to(root.resolve())
    ):
        raise ResourceError(f"captured release-upgrade path is unavailable: {relative}")
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if manifest.get(relative) != sha256:
        raise ResourceError(f"release-upgrade input is not pinned exactly: {relative}")
    return {"path": relative, "sha256": sha256}


def build_release_upgrade_input(
    command: dict[str, Any],
    *,
    workspace: Path,
    manifest: dict[str, str],
    authority: str,
    request: str,
    stage: str,
    epoch: int,
    recipe_digest: str,
) -> dict[str, Any]:
    validate_compiled_command(command)
    candidate_digest = digest(manifest)
    body = {
        "operation": RELEASE_UPGRADE_KIND,
        "authority": authority,
        "request": request,
        "stage": stage,
        "epoch": epoch,
        "recipe_digest": recipe_digest,
        "candidate_digest": candidate_digest,
        "previous_boundary": command["previous_boundary"],
        "adapter": _captured(workspace, command["adapter"], manifest),
        "fixtures": [
            _captured(workspace, item, manifest) for item in command["fixtures"]
        ],
        "pre_assertions": [
            _captured(workspace, item, manifest)
            for item in command["pre_assertions"]
        ],
        "post_assertions": [
            _captured(workspace, item, manifest)
            for item in command["post_assertions"]
        ],
        "readbacks": [
            _captured(workspace, item, manifest) for item in command["readbacks"]
        ],
    }
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": RELEASE_UPGRADE_INPUT_KIND,
        **body,
        "contract_digest": digest(body),
    }


def _bounded_text(value: Any, field: str, maximum: int = 160) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
    ):
        raise ResourceError(f"release-upgrade {field} is not a bounded string")
    return value


def validate_release_upgrade_result(
    value: Any, expected: dict[str, Any]
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "contract_digest",
        "candidate_digest",
        "previous_boundary",
        "status",
        "phases",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ResourceError("unsupported release-upgrade result fields")
    if value.get("schema_version") != core.SCHEMA_VERSION:
        raise ResourceError("unsupported release-upgrade result schema version")
    if value.get("kind") != RELEASE_UPGRADE_RESULT_KIND:
        raise ResourceError("unsupported release-upgrade result kind")
    for field in ("contract_digest", "candidate_digest"):
        if value.get(field) != expected[field] or not _DIGEST.fullmatch(value[field]):
            raise ResourceError(f"release-upgrade result {field} mismatch")
    if value.get("previous_boundary") != expected["previous_boundary"]:
        raise ResourceError("release-upgrade result previous boundary mismatch")
    if value.get("status") not in {"passed", "failed"}:
        raise ResourceError("release-upgrade result status must be passed or failed")
    phases = value.get("phases")
    if not isinstance(phases, list) or not phases or len(phases) > len(
        RELEASE_UPGRADE_PHASES
    ):
        raise ResourceError("release-upgrade result requires a bounded phase prefix")
    normalized = []
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict) or set(phase) != {
            "name",
            "status",
            "exit_code",
            "evidence",
        }:
            raise ResourceError("unsupported release-upgrade phase fields")
        if phase.get("name") != RELEASE_UPGRADE_PHASES[index]:
            raise ResourceError("release-upgrade phases must use the declared order")
        status = phase.get("status")
        exit_code = phase.get("exit_code")
        if status not in {"passed", "failed"} or type(exit_code) is not int:
            raise ResourceError("release-upgrade phase status/exit code is invalid")
        if (status == "passed") != (exit_code == 0):
            raise ResourceError("release-upgrade phase status contradicts exit code")
        evidence = phase.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 64:
            raise ResourceError("release-upgrade phase evidence must be bounded")
        records = []
        for record in evidence:
            if not isinstance(record, dict) or set(record) != {
                "name",
                "status",
                "sha256",
                "size_bytes",
            }:
                raise ResourceError("unsupported release-upgrade evidence fields")
            name = _bounded_text(record.get("name"), "evidence name")
            if record.get("status") not in {"passed", "failed"}:
                raise ResourceError("release-upgrade evidence status is invalid")
            if not isinstance(record.get("sha256"), str) or not _DIGEST.fullmatch(
                record["sha256"]
            ):
                raise ResourceError("release-upgrade evidence digest is invalid")
            size = record.get("size_bytes")
            if type(size) is not int or size < 0 or size > 8 * 1024 * 1024:
                raise ResourceError("release-upgrade evidence size is invalid")
            records.append({**record, "name": name})
        normalized.append({**phase, "evidence": records})
    if value["status"] == "passed":
        if len(normalized) != len(RELEASE_UPGRADE_PHASES) or any(
            phase["status"] != "passed" for phase in normalized
        ):
            raise ResourceError("passed release-upgrade result requires every phase")
    elif normalized[-1]["status"] != "failed":
        raise ResourceError("failed release-upgrade result must end at a failed phase")
    return {**value, "phases": normalized}


def read_release_upgrade_result(
    path: Path, expected: dict[str, Any], *, maximum_bytes: int = 1024 * 1024
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ResourceError("release-upgrade result must not be a symlink")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or (
                (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise ResourceError("release-upgrade result must be a regular file")
            raw = stream.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ResourceError("release-upgrade result exceeds the size limit")
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except FileNotFoundError as error:
        raise ResourceError(
            "release-upgrade adapter did not create a result"
        ) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise ResourceError("release-upgrade result is unreadable") from error
    normalized = validate_release_upgrade_result(value, expected)
    return normalized, {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "status": normalized["status"],
    }
