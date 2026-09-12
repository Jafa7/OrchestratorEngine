"""Point-in-time admission checks for wake-enabled operation delivery."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import binding, core, host_capabilities

KIND = "ORCHESTRATOR_DELIVERY_PREFLIGHT"
HISTORY_KIND = "ORCHESTRATOR_DELIVERY_PREFLIGHT_HISTORY"
MODES = frozenset({"off", "warn", "require-ready"})
STATUSES = frozenset({"ready", "not_ready", "unknown", "not_checked"})
OPERATION_KINDS = frozenset(
    {
        "github_actions",
        "github_pull_request",
        "local_check",
        "continuity",
        "worker",
        "workstream",
    }
)
DEFAULT_MODE = "warn"
MAX_OPERATION_ID_LENGTH = 256
MAX_HISTORY_LIMIT = 100
CONFIG_NAME = "workers.toml"
MAX_DETAIL_LENGTH = 1000
MAX_STATUS_LENGTH = 64


class DeliveryPreflightError(RuntimeError):
    """A deterministic completion-delivery admission failure."""


def _bounded_text(value: object, *, limit: int = MAX_DETAIL_LENGTH) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def _bounded_nonnegative_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value >= 0 and math.isfinite(value) else None


def validate_mode(value: object) -> str:
    if not isinstance(value, str) or value not in MODES:
        raise DeliveryPreflightError(
            "completion delivery mode must be one of: " + ", ".join(sorted(MODES))
        )
    return value


def mode_from_dispatch(dispatch: dict[str, Any]) -> str:
    return validate_mode(dispatch.get("completion_delivery_mode", DEFAULT_MODE))


def configured_mode(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> str:
    path = project_root.expanduser().resolve() / state_dir / CONFIG_NAME
    if not path.is_file():
        return DEFAULT_MODE
    try:
        value = tomllib.loads(core.read_config_text(path))
    except tomllib.TOMLDecodeError as error:
        raise DeliveryPreflightError(
            f"invalid workers config: {path}: {error}"
        ) from error
    dispatch = value.get("dispatch", {})
    if not isinstance(dispatch, dict):
        raise DeliveryPreflightError("workers config [dispatch] must be a table")
    return mode_from_dispatch(dispatch)


def resolve_mode(
    project_root: Path,
    *,
    requested: str | None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> str:
    return validate_mode(requested) if requested is not None else configured_mode(
        project_root, state_dir=state_dir
    )


def validate_operation_kind(value: object) -> str:
    if not isinstance(value, str) or value not in OPERATION_KINDS:
        raise DeliveryPreflightError(
            "operation kind must be one of: "
            + ", ".join(sorted(OPERATION_KINDS))
        )
    return value


def validate_operation_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DeliveryPreflightError("operation id must be a non-empty string")
    if len(value) > MAX_OPERATION_ID_LENGTH:
        raise DeliveryPreflightError(
            f"operation id must be at most {MAX_OPERATION_ID_LENGTH} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DeliveryPreflightError("operation id contains control characters")
    return value


def artifact_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return project_root.expanduser().resolve() / state_dir / "delivery-preflights"


def operation_key(operation_kind: str, operation_id: str) -> str:
    kind = validate_operation_kind(operation_kind)
    identifier = validate_operation_id(operation_id)
    return hashlib.sha256(f"{kind}\0{identifier}".encode()).hexdigest()


def operation_dir(
    project_root: Path,
    *,
    operation_kind: str,
    operation_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    kind = validate_operation_kind(operation_kind)
    key = operation_key(kind, operation_id)
    root = artifact_root(project_root, state_dir=state_dir)
    kind_root = root / kind
    directory = kind_root / key
    if root.is_symlink() or kind_root.is_symlink() or directory.is_symlink():
        raise DeliveryPreflightError(
            "delivery preflight artifact directories must not be symbolic links"
        )
    return directory


def _binding_match(
    project_root: Path,
    *,
    wake_target: dict[str, Any],
    state_dir: str,
) -> tuple[bool | None, str | None]:
    try:
        bound = binding.load_binding(project_root, state_dir=state_dir)
    except (OSError, RuntimeError, ValueError) as error:
        return None, _bounded_text(f"current host binding could not be read: {error}")
    if bound is None:
        return False, "current host binding is absent"
    current = binding.wake_target_from_binding(bound)
    if binding.same_wake_destination(current, wake_target):
        return True, None
    return False, "operation wake target differs from the current host binding"


def _channel_probe(
    project_root: Path,
    *,
    wake_target: dict[str, Any] | None,
    state_dir: str,
) -> dict[str, Any]:
    if wake_target is None:
        return {
            "status": "not_ready",
            "reason_code": "no_binding",
            "detail": "no wake target is configured for this operation",
            "binding_match": False,
            "warnings": ["Bind a host before wake-enabled dispatch."],
        }
    try:
        binding.validate_wake_target(wake_target)
    except binding.BindingError as error:
        return {
            "status": "not_ready",
            "reason_code": "invalid_wake_target",
            "detail": _bounded_text(error),
            "binding_match": None,
            "warnings": ["Repair or recapture the operation wake target."],
        }
    host = str(wake_target["host"])
    capabilities = host_capabilities.for_host(host)
    matched, binding_warning = _binding_match(
        project_root,
        wake_target=wake_target,
        state_dir=state_dir,
    )
    warnings = [_bounded_text(binding_warning)] if binding_warning else []
    try:
        if host == "claude":
            from . import claude_stream

            channel = claude_stream.stream_status(
                [project_root], state_dir=state_dir
            )
            channel_status = _bounded_text(
                channel.get("status"), limit=MAX_STATUS_LENGTH
            )
            ready = channel_status == "fresh" and channel.get("healthy") is True
            probe = {
                "channel_status": channel_status,
                "age_seconds": _bounded_nonnegative_number(
                    channel.get("age_seconds")
                ),
                "max_age_seconds": _bounded_nonnegative_number(
                    channel.get("max_age_seconds")
                ),
            }
            reason_code = "stream_fresh" if ready else f"stream_{channel_status}"
            detail = f"claude stream is {channel_status}"
        else:
            from . import watcher

            channel = watcher.service_status(
                [project_root], state_dir=state_dir, host=host
            )
            channel_status = _bounded_text(
                channel.get("status"), limit=MAX_STATUS_LENGTH
            )
            service_action = _bounded_text(
                channel.get("action"), limit=MAX_STATUS_LENGTH
            )
            host_filter_value = channel.get("host_filter")
            host_filter = (
                [
                    item
                    for item in host_filter_value
                    if isinstance(item, str) and item in binding.SUPPORTED_HOSTS
                ][: len(binding.SUPPORTED_HOSTS)]
                if isinstance(host_filter_value, list)
                else None
            )
            callback_action = service_action in watcher.CALLBACK_ACTIONS
            host_routed = host_filter is None or host in host_filter
            ready = (
                channel_status == "running"
                and channel.get("heartbeat_healthy") is True
                and callback_action
                and host_routed
                and not channel.get("warnings")
            )
            probe = {
                "channel_status": channel_status,
                "service_action": service_action,
                "host_filter": host_filter,
                "heartbeat_status": _bounded_text(
                    channel.get("heartbeat_status"), limit=MAX_STATUS_LENGTH
                )
                if channel.get("heartbeat_status") is not None
                else None,
                "heartbeat_age_seconds": _bounded_nonnegative_number(
                    channel.get("heartbeat_age_seconds")
                ),
                "process_identity_status": _bounded_text(
                    channel.get("process_identity_status"), limit=MAX_STATUS_LENGTH
                )
                if channel.get("process_identity_status") is not None
                else None,
            }
            if ready:
                reason_code = "service_running"
                detail = f"{host} callback service is running"
            elif channel_status != "running":
                reason_code = f"service_{channel_status}"
                detail = f"{host} callback service is {channel_status}"
            elif not callback_action:
                reason_code = "service_action_not_callback"
                detail = f"{host} service action is not a callback action"
            elif not host_routed:
                reason_code = "service_host_not_routed"
                detail = f"{host} is not included in the service host filter"
            else:
                reason_code = "service_warning"
                detail = f"{host} callback service has an actionable warning"
    except (OSError, RuntimeError, ValueError) as error:
        return {
            "status": "unknown",
            "reason_code": "probe_error",
            "detail": _bounded_text(error),
            "host": host,
            "channel_lifecycle": capabilities["channel_lifecycle"],
            "binding_match": matched,
            "warnings": [*warnings, "Completion delivery readiness is unknown."],
        }
    if not ready:
        warnings.append(
            "Arm or repair the completion channel before ending the host turn."
        )
    return {
        "status": "ready" if ready else "not_ready",
        "reason_code": reason_code,
        "detail": detail,
        "host": host,
        "channel_lifecycle": capabilities["channel_lifecycle"],
        "binding_match": matched,
        "probe": probe,
        "warnings": warnings,
    }


def run(
    project_root: Path,
    *,
    operation_kind: str,
    operation_id: str,
    wake_policy: str,
    wake_target: dict[str, Any] | None,
    mode: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
    now: datetime | None = None,
    probe_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    kind = validate_operation_kind(operation_kind)
    identifier = validate_operation_id(operation_id)
    selected_mode = resolve_mode(project, requested=mode, state_dir=state_dir)
    if wake_policy == "never":
        return {
            "mode": selected_mode,
            "status": "not_checked",
            "reason_code": "wake_disabled",
            "point_in_time": True,
        }
    if selected_mode == "off":
        return {
            "mode": selected_mode,
            "status": "not_checked",
            "reason_code": "mode_off",
            "point_in_time": True,
        }
    checked_at = (now or datetime.now(UTC)).isoformat(timespec="milliseconds")
    preflight_id = str(uuid.uuid4())
    cache_key = hashlib.sha256(
        json.dumps(wake_target, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    probe = probe_cache.get(cache_key) if probe_cache is not None else None
    if probe is None:
        probe = _channel_probe(project, wake_target=wake_target, state_dir=state_dir)
        if probe_cache is not None:
            probe_cache[cache_key] = deepcopy(probe)
    report = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": KIND,
        "preflight_id": preflight_id,
        "operation_kind": kind,
        "operation_id": identifier,
        "mode": selected_mode,
        "wake_policy": wake_policy,
        "checked_at": checked_at,
        "point_in_time": True,
        **deepcopy(probe),
    }
    directory = operation_dir(
        project,
        operation_kind=kind,
        operation_id=identifier,
        state_dir=state_dir,
    )
    path = directory / f"{preflight_id}.json"
    core.atomic_json(path, report)
    return {**report, "artifact_path": str(path)}


def enforce(report: dict[str, Any]) -> None:
    if report.get("mode") == "require-ready" and report.get("status") != "ready":
        reason = report.get("reason_code", "unknown")
        path = report.get("artifact_path")
        suffix = f"; evidence: {path}" if isinstance(path, str) else ""
        raise DeliveryPreflightError(
            f"completion delivery is not ready ({reason}) under mode "
            f"require-ready{suffix}"
        )


def attach(output: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Attach bounded dispatch-only metadata without changing durable descriptors."""

    result = {**output, "completion_delivery": report}
    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        result["warnings"] = [*result.get("warnings", []), *warnings]
    return result


def history(
    project_root: Path,
    *,
    operation_kind: str,
    operation_id: str,
    limit: int = 20,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_HISTORY_LIMIT
    ):
        raise DeliveryPreflightError(
            f"history limit must be between 1 and {MAX_HISTORY_LIMIT}"
        )
    directory = operation_dir(
        project_root,
        operation_kind=operation_kind,
        operation_id=operation_id,
        state_dir=state_dir,
    )
    entries: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        if path.is_symlink():
            continue
        try:
            value = core.load_object(path)
        except (OSError, RuntimeError, ValueError):
            continue
        if value.get("kind") != KIND or not core.is_supported_schema_version(
            value.get("schema_version")
        ):
            continue
        entries.append({**value, "artifact_path": str(path)})
    entries.sort(
        key=lambda item: (
            str(item.get("checked_at", "")),
            str(item.get("preflight_id", "")),
        ),
        reverse=True,
    )
    selected = entries[:limit]
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": HISTORY_KIND,
        "operation_kind": validate_operation_kind(operation_kind),
        "operation_id": validate_operation_id(operation_id),
        "count": len(selected),
        "total_count": len(entries),
        "entries": selected,
    }


def latest(
    project_root: Path,
    *,
    operation_kind: str,
    operation_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any] | None:
    report = history(
        project_root,
        operation_kind=operation_kind,
        operation_id=operation_id,
        limit=1,
        state_dir=state_dir,
    )
    entries = report["entries"]
    return entries[0] if entries else None


def prune(
    project_root: Path,
    *,
    cutoff_timestamp: float,
    state_dir: str = core.DEFAULT_STATE_DIR,
    dry_run: bool = False,
) -> list[str]:
    """Prune old history while retaining the newest sample per operation key."""

    removed: list[str] = []
    root = artifact_root(project_root, state_dir=state_dir)
    if root.is_symlink():
        return removed
    for kind in sorted(OPERATION_KINDS):
        kind_root = root / kind
        if kind_root.is_symlink():
            continue
        for directory in sorted(kind_root.glob("*")):
            if directory.is_symlink() or not directory.is_dir():
                continue
            paths = sorted(
                (path for path in directory.glob("*.json") if path.is_file()),
                key=lambda path: (path.stat().st_mtime, path.name),
                reverse=True,
            )
            for path in paths[1:]:
                if path.stat().st_mtime > cutoff_timestamp:
                    continue
                removed.append(str(path))
                if not dry_run:
                    path.unlink()
    return removed
