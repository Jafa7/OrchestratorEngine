"""CLI worker registry and detached runner.

The host chat dispatches a task with `worker run`, which returns immediately so
the chat turn can end. A detached supervisor process runs the worker CLI,
captures its output, and emits the standard terminal event + inbox signal on
exit. The configured host channel decides how that completion is delivered.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    binding,
    core,
    task_resolution,
    telemetry_adapters,
    worker_diagnostics,
    worker_lease,
    worker_policy,
)

WORKERS_CONFIG_NAME = "workers.toml"
PROMPT_MODES = {"arg", "stdin"}
AVAILABILITY_MODES = {"off", "block-unavailable", "require-available"}
INTENT_ENFORCEMENT_MODES = {"off", "permissions", "strict"}
TASK_KIND = "WORKER_TASK"
RESERVED_KEYS = {
    "enabled",
    "command",
    "prompt_via",
    "timeout_seconds",
    "expect_long_running",
    "availability_probe",
    "availability_timeout_seconds",
    "policy",
    "max_concurrent",
    "max_no_progress_seconds",
    "soft_duration_seconds",
    "soft_output_bytes",
    "soft_token_budget",
    "usage_adapter",
    "admission",
}
# Workers may legitimately run for hours with no configured timeout; the
# supervisor refreshes the task descriptor on this cadence so long tasks stay
# observable (`last_alive_at`) instead of looking stuck.
TASK_HEARTBEAT_INTERVAL_SECONDS = 30.0
MAX_AVAILABILITY_TIMEOUT_SECONDS = 300.0
# A timed-out worker is asked to stop with a process-group SIGTERM and gets this
# long to exit on its own before the group is force-killed.
WORKER_TERMINATION_GRACE_SECONDS = 10.0
# Bound on how long forced termination may take before the supervisor gives up
# waiting and finalizes the task anyway.
WORKER_TERMINATION_TIMEOUT_SECONDS = 10.0
WORKER_TERMINATION_POLL_SECONDS = 0.1
CONTROL_POLL_SECONDS = 1.0
MAX_DECLARED_OUTPUT_FILES = 64
MAX_DECLARED_OUTPUT_FILE_BYTES = 4 * 1024 * 1024
MAX_DECLARED_OUTPUT_TOTAL_BYTES = 16 * 1024 * 1024
MAX_WAIT_TASKS = 64
WAIT_MODES = {"all", "any"}
HANDOFF_LIST_LIMITS = {"evidence": 64, "risks": 32, "next_actions": 32}
_DETACHED_PROCESSES: list[Any] = []
INTENT_ENUMS = {
    "role": {
        "implementation",
        "review",
        "verification",
        "architecture",
        "triage",
        "documentation",
    },
    "risk": {"low", "medium", "high"},
    "verification": {"structural", "focused", "full"},
    "permissions": {"readonly", "restricted", "full"},
}


class WorkerError(RuntimeError):
    """A deterministic worker registry or runner failure."""


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def collect_declared_outputs(output_dir: Path, task_dir: Path) -> dict[str, Any]:
    """Hash bounded task-local outputs without following symlinks."""
    files: list[dict[str, Any]] = []
    total_bytes = 0
    if output_dir.is_symlink():
        raise WorkerError("declared output directory must not be a symlink")
    for path in sorted(output_dir.rglob("*")) if output_dir.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        if len(files) >= MAX_DECLARED_OUTPUT_FILES:
            raise WorkerError(
                f"declared outputs exceed {MAX_DECLARED_OUTPUT_FILES} files"
            )
        size = path.stat().st_size
        if size > MAX_DECLARED_OUTPUT_FILE_BYTES:
            raise WorkerError(
                "declared output exceeds "
                f"{MAX_DECLARED_OUTPUT_FILE_BYTES} bytes: {path}"
            )
        total_bytes += size
        if total_bytes > MAX_DECLARED_OUTPUT_TOTAL_BYTES:
            raise WorkerError(
                f"declared outputs exceed {MAX_DECLARED_OUTPUT_TOTAL_BYTES} total bytes"
            )
        files.append(
            {
                "path": str(path.relative_to(task_dir)),
                "bytes": size,
                "sha256": core.sha256_file(path),
            }
        )
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_OUTPUT_MANIFEST",
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": files,
        "captured_at": core.utc_now(),
    }


def validate_worker_handoff(handoff: dict[str, Any]) -> None:
    """Validate the bounded runtime subset of the public handoff schema."""
    if handoff.get("kind") != "WORKER_HANDOFF":
        raise WorkerError("worker handoff has unexpected kind")
    if handoff.get("schema_version") != core.SCHEMA_VERSION:
        raise WorkerError("worker handoff has unsupported schema_version")
    summary = handoff.get("summary")
    if not isinstance(summary, str) or len(summary) > 4096:
        raise WorkerError("worker handoff summary is missing or too large")
    for field, maximum in HANDOFF_LIST_LIMITS.items():
        value = handoff.get(field)
        if value is not None and (
            not isinstance(value, list) or len(value) > maximum
        ):
            raise WorkerError(
                f"worker handoff {field} must be an array with at most "
                f"{maximum} items"
            )


def remember_detached_process(process: Any) -> None:
    """Keep detached children reachable and reap completed ones opportunistically."""
    _DETACHED_PROCESSES[:] = [
        child for child in _DETACHED_PROCESSES if child.poll() is None
    ]
    _DETACHED_PROCESSES.append(process)


def load_task_intent(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, None
    intent_path = core.ensure_file(path.expanduser().resolve(), field="task intent")
    intent = core.load_object(intent_path)
    unknown = sorted(
        set(intent) - set(INTENT_ENUMS) - {"authorizations", "schema_version", "kind"}
    )
    if unknown:
        raise WorkerError(f"task intent contains unknown fields: {', '.join(unknown)}")
    if (
        intent.get("schema_version", 1) != 1
        or intent.get("kind", "WORKER_TASK_INTENT") != "WORKER_TASK_INTENT"
    ):
        raise WorkerError("task intent has unsupported schema_version or kind")
    for key, allowed in INTENT_ENUMS.items():
        value = intent.get(key)
        if value is not None and value not in allowed:
            raise WorkerError(
                f"task intent {key} must be one of: {', '.join(sorted(allowed))}"
            )
    authorizations = intent.get("authorizations", {})
    if not isinstance(authorizations, dict) or any(
        key not in {"commit", "push", "network"} or not isinstance(value, bool)
        for key, value in authorizations.items()
    ):
        raise WorkerError(
            "task intent authorizations must contain boolean commit/push/network fields"
        )
    normalized = {**intent, "schema_version": 1, "kind": "WORKER_TASK_INTENT"}
    return normalized, canonical_sha256(normalized)


def workers_config_path(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / WORKERS_CONFIG_NAME


def tasks_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "tasks"


def task_dir_for(
    project_root: Path,
    task_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    if not task_id or "/" in task_id or "\\" in task_id or task_id.startswith("."):
        raise WorkerError(f"invalid task id: {task_id!r}")
    return tasks_root(project_root, state_dir=state_dir) / task_id


def load_registry(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, dict[str, Any]]:
    path = workers_config_path(project_root, state_dir=state_dir)
    if not path.is_file():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise WorkerError(f"invalid workers config: {path}: {error}") from error
    workers = value.get("workers")
    if not isinstance(workers, dict):
        raise WorkerError(f"workers config must contain a [workers.*] table: {path}")
    try:
        policies = worker_policy.load_policies(path, value.get("policies"))
    except worker_policy.WorkerPolicyError as error:
        raise WorkerError(str(error)) from error
    registry: dict[str, dict[str, Any]] = {}
    for name, config in workers.items():
        validated = validate_worker_config(name, config)
        policy_name = validated["policy"]
        if policy_name is not None and policy_name not in policies:
            raise WorkerError(f"worker {name} references unknown policy: {policy_name}")
        validated["policy_config"] = (
            policies[policy_name] if policy_name is not None else None
        )
        validated["bundled_policy"] = None
        if validated["policy_config"] is not None:
            try:
                materials = worker_policy.read_policy_materials(
                    project_root,
                    validated["policy_config"],
                )
                validated["bundled_policy"] = worker_policy.bundled_policy_status(
                    str(policy_name),
                    materials,
                )
            except worker_policy.WorkerPolicyError as error:
                validated["diagnostics"].append(
                    worker_diagnostics.diagnostic(
                        code="worker_policy_unreadable",
                        severity="error",
                        message=f"worker {name} policy is not dispatchable: {error}",
                        suggested_action=(
                            "Repair the configured policy files before a new "
                            "dispatch. Existing tasks use their saved effective "
                            "prompt snapshots."
                        ),
                    )
                )
                validated["warnings"] = worker_diagnostics.filter_diagnostics(
                    validated["diagnostics"],
                    minimum_severity="warning",
                )
        registry[name] = validated
    return registry


def load_dispatch_config(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    path = workers_config_path(project_root, state_dir=state_dir)
    if not path.is_file():
        return {
            "max_concurrent": None,
            "enforce_intent": False,
            "intent_enforcement": "off",
            "availability_mode": "off",
        }
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise WorkerError(f"invalid workers config: {path}: {error}") from error
    dispatch = value.get("dispatch", {})
    if not isinstance(dispatch, dict):
        raise WorkerError("workers config [dispatch] must be a table")
    limit = dispatch.get("max_concurrent")
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
    ):
        raise WorkerError("dispatch max_concurrent must be a positive integer")
    enforce_intent = dispatch.get("enforce_intent", False)
    if not isinstance(enforce_intent, bool):
        raise WorkerError("dispatch enforce_intent must be a boolean")
    if "enforce_intent" in dispatch and "intent_enforcement" in dispatch:
        raise WorkerError(
            "dispatch cannot specify both enforce_intent and intent_enforcement"
        )
    intent_enforcement = dispatch.get(
        "intent_enforcement", "permissions" if enforce_intent else "off"
    )
    if intent_enforcement not in INTENT_ENFORCEMENT_MODES:
        raise WorkerError(
            "dispatch intent_enforcement must be one of: "
            + ", ".join(sorted(INTENT_ENFORCEMENT_MODES))
        )
    availability_mode = dispatch.get("availability_mode", "off")
    if availability_mode not in AVAILABILITY_MODES:
        raise WorkerError(
            "dispatch availability_mode must be one of: "
            + ", ".join(sorted(AVAILABILITY_MODES))
        )
    return {
        "max_concurrent": limit,
        "enforce_intent": intent_enforcement != "off",
        "intent_enforcement": intent_enforcement,
        "availability_mode": availability_mode,
    }


def enforce_task_intent(
    project_root: Path,
    *,
    intent: dict[str, Any] | None,
    config: dict[str, Any],
    state_dir: str,
) -> dict[str, Any] | None:
    dispatch = load_dispatch_config(project_root, state_dir=state_dir)
    mode = dispatch["intent_enforcement"]
    if mode == "off" or intent is None:
        return None
    requested = intent.get("permissions")
    if isinstance(requested, str):
        configured = config.get("extras", {}).get("permission_profile")
        if configured not in {"readonly", "restricted", "full"}:
            raise WorkerError(
                "intent enforcement requires worker permission_profile metadata"
            )
        rank = {"readonly": 0, "restricted": 1, "full": 2}
        if rank[str(configured)] > rank[requested]:
            raise WorkerError(
                f"worker permission_profile {configured} exceeds task intent "
                f"{requested}"
            )
    if mode != "strict":
        return {
            "mode": mode,
            "evaluated_at": core.utc_now(),
            "permission_profile": config.get("extras", {}).get(
                "permission_profile"
            ),
        }
    admission_fields = {"role", "risk", "verification", "authorizations"}
    needs_admission = any(key in intent for key in admission_fields)
    admission = config.get("admission")
    if needs_admission and not isinstance(admission, dict):
        raise WorkerError(
            "strict intent enforcement requires worker admission metadata"
        )
    if not isinstance(admission, dict):
        admission = {}
    role = intent.get("role")
    if isinstance(role, str):
        roles = admission.get("roles")
        if roles is None:
            raise WorkerError("strict intent role requires worker admission roles")
        if role not in roles:
            raise WorkerError(f"worker admission roles do not include task role {role}")
    risk = intent.get("risk")
    if isinstance(risk, str):
        maximum = admission.get("max_risk")
        if maximum is None:
            raise WorkerError("strict intent risk requires worker admission max_risk")
        risk_rank = {"low": 0, "medium": 1, "high": 2}
        if risk_rank[risk] > risk_rank[maximum]:
            raise WorkerError(f"task risk {risk} exceeds worker max_risk {maximum}")
    verification = intent.get("verification")
    if isinstance(verification, str):
        supported = admission.get("verification")
        if supported is None:
            raise WorkerError(
                "strict intent verification requires worker admission verification"
            )
        if verification not in supported:
            raise WorkerError(
                "worker admission verification does not include task verification "
                f"{verification}"
            )
    if "authorizations" in intent:
        configured_authorizations = admission.get("authorizations")
        if configured_authorizations is None:
            raise WorkerError(
                "strict intent authorizations require worker admission authorizations"
            )
        requested_authorizations = intent.get("authorizations", {})
        for key in ("commit", "push", "network"):
            configured_value = configured_authorizations.get(key, False)
            requested_value = requested_authorizations.get(key, False)
            if configured_value and not requested_value:
                raise WorkerError(
                    f"worker admission authorization {key} exceeds task intent"
                )
    return {
        "mode": mode,
        "evaluated_at": core.utc_now(),
        "permission_profile": config.get("extras", {}).get("permission_profile"),
        "worker_admission": admission,
    }


def validate_admission(name: str, value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WorkerError(f"worker {name} admission must be a table")
    allowed_keys = {"roles", "max_risk", "verification", "authorizations"}
    unknown = sorted(set(value) - allowed_keys)
    if unknown:
        raise WorkerError(
            f"worker {name} admission contains unknown fields: {', '.join(unknown)}"
        )
    normalized: dict[str, Any] = {}
    for key, allowed in (
        ("roles", INTENT_ENUMS["role"]),
        ("verification", INTENT_ENUMS["verification"]),
    ):
        items = value.get(key)
        if items is None:
            continue
        if (
            not isinstance(items, list)
            or not items
            or not all(isinstance(item, str) and item in allowed for item in items)
            or len(set(items)) != len(items)
        ):
            raise WorkerError(
                f"worker {name} admission {key} must be a non-empty unique list of: "
                + ", ".join(sorted(allowed))
            )
        normalized[key] = list(items)
    max_risk = value.get("max_risk")
    if max_risk is not None:
        if max_risk not in INTENT_ENUMS["risk"]:
            raise WorkerError(
                f"worker {name} admission max_risk must be one of: low, medium, high"
            )
        normalized["max_risk"] = max_risk
    authorizations = value.get("authorizations")
    if authorizations is not None:
        if not isinstance(authorizations, dict) or any(
            key not in {"commit", "push", "network"} or not isinstance(item, bool)
            for key, item in authorizations.items()
        ):
            raise WorkerError(
                f"worker {name} admission authorizations must contain only boolean "
                "commit/push/network fields"
            )
        normalized["authorizations"] = dict(authorizations)
    return normalized


def validate_worker_config(name: str, config: object) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise WorkerError(f"worker {name} config must be a table")
    command = config.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise WorkerError(f"worker {name} requires a non-empty command list")
    prompt_via = config.get("prompt_via", "arg")
    if prompt_via not in PROMPT_MODES:
        raise WorkerError(f"worker {name} has unsupported prompt_via: {prompt_via}")
    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise WorkerError(f"worker {name} enabled flag must be a boolean")
    timeout_seconds = config.get("timeout_seconds")
    if timeout_seconds is not None and (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise WorkerError(f"worker {name} timeout_seconds must be positive")
    expect_long_running = config.get("expect_long_running", False)
    if not isinstance(expect_long_running, bool):
        raise WorkerError(f"worker {name} expect_long_running flag must be a boolean")
    policy = config.get("policy")
    if policy is not None and (not isinstance(policy, str) or not policy.strip()):
        raise WorkerError(f"worker {name} policy must be a non-empty string")
    max_concurrent = config.get("max_concurrent")
    if max_concurrent is not None and (
        not isinstance(max_concurrent, int)
        or isinstance(max_concurrent, bool)
        or max_concurrent <= 0
    ):
        raise WorkerError(f"worker {name} max_concurrent must be a positive integer")
    numeric_limits: dict[str, int | float | None] = {}
    for key in (
        "max_no_progress_seconds",
        "soft_duration_seconds",
        "soft_output_bytes",
        "soft_token_budget",
    ):
        limit = config.get(key)
        if limit is not None and (
            not isinstance(limit, (int, float))
            or isinstance(limit, bool)
            or not math.isfinite(limit)
            or limit <= 0
        ):
            raise WorkerError(f"worker {name} {key} must be positive and finite")
        numeric_limits[key] = limit
    usage_adapter = config.get("usage_adapter")
    if usage_adapter is not None and (
        not isinstance(usage_adapter, str) or not usage_adapter.strip()
    ):
        raise WorkerError(f"worker {name} usage_adapter must be a non-empty string")
    if (
        usage_adapter is not None
        and usage_adapter not in telemetry_adapters.USAGE_ADAPTERS
    ):
        raise WorkerError(f"worker {name} has unknown usage_adapter: {usage_adapter}")
    availability_probe = config.get("availability_probe")
    if availability_probe is not None and (
        not isinstance(availability_probe, list)
        or not availability_probe
        or not all(isinstance(item, str) and item for item in availability_probe)
    ):
        raise WorkerError(
            f"worker {name} availability_probe requires a non-empty command list"
        )
    availability_timeout = config.get("availability_timeout_seconds")
    if availability_probe is not None and (
        not isinstance(availability_timeout, (int, float))
        or isinstance(availability_timeout, bool)
        or not math.isfinite(availability_timeout)
        or availability_timeout <= 0
        or availability_timeout > MAX_AVAILABILITY_TIMEOUT_SECONDS
    ):
        raise WorkerError(
            f"worker {name} availability_timeout_seconds must be finite and between "
            f"0 and {MAX_AVAILABILITY_TIMEOUT_SECONDS:g} seconds"
        )
    admission = validate_admission(name, config.get("admission"))
    extras = {key: value for key, value in config.items() if key not in RESERVED_KEYS}
    diagnostics = worker_diagnostics.evaluate_profile(
        name=name,
        command=list(command),
        prompt_via=str(prompt_via),
        timeout_seconds=timeout_seconds,
        expect_long_running=expect_long_running,
        availability_probe=availability_probe,
        availability_timeout_seconds=availability_timeout,
    )
    if policy is None:
        diagnostics.append(
            worker_diagnostics.diagnostic(
                code="worker_policy_not_configured",
                severity="info",
                message=(
                    f"worker {name} has no composed behavior policy; only its "
                    "task prompt and provider-local instructions will apply"
                ),
                suggested_action=(
                    "Assign an explicit [policies.*] entry when deterministic "
                    "quality/economy behavior is required."
                ),
            )
        )
    return {
        "name": name,
        "enabled": enabled,
        "command": list(command),
        "prompt_via": prompt_via,
        "timeout_seconds": timeout_seconds,
        "expect_long_running": expect_long_running,
        "policy": policy,
        "max_concurrent": max_concurrent,
        "usage_adapter": usage_adapter,
        **numeric_limits,
        "availability_probe": list(availability_probe) if availability_probe else None,
        "availability_timeout_seconds": availability_timeout,
        "admission": admission,
        "extras": extras,
        "diagnostics": diagnostics,
        "warnings": worker_diagnostics.filter_diagnostics(
            diagnostics,
            minimum_severity="warning",
        ),
    }


def worker_profile_warnings(
    *,
    name: str,
    command: list[str],
    prompt_via: str,
) -> list[dict[str, str]]:
    """Return advisory diagnostics for profiles likely to need interaction.

    These warnings are intentionally non-blocking. Permission and autonomy
    flags are provider-specific, so the engine surfaces known sharp edges
    without rewriting commands or treating them as core policy.
    """
    return worker_diagnostics.filter_diagnostics(
        worker_diagnostics.evaluate_profile(
            name=name,
            command=command,
            prompt_via=prompt_via,
            timeout_seconds=None,
            expect_long_running=True,
        ),
        minimum_severity="warning",
    )


def require_worker(
    project_root: Path,
    worker: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    registry = load_registry(project_root, state_dir=state_dir)
    if not registry:
        raise WorkerError(
            "no workers configured; create "
            f"{workers_config_path(project_root, state_dir=state_dir)}"
        )
    config = registry.get(worker)
    if config is None:
        raise WorkerError(
            f"unknown worker: {worker}; configured: {', '.join(sorted(registry))}"
        )
    if not config["enabled"]:
        raise WorkerError(f"worker {worker} is disabled")
    return config


def list_workers(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    registry = load_registry(project_root, state_dir=state_dir)
    return {
        "schema_version": core.SCHEMA_VERSION,
        "config_path": str(workers_config_path(project_root, state_dir=state_dir)),
        "dispatch": load_dispatch_config(project_root, state_dir=state_dir),
        "workers": {
            name: {
                "enabled": config["enabled"],
                "command": config["command"],
                "prompt_via": config["prompt_via"],
                "timeout_seconds": config["timeout_seconds"],
                "expect_long_running": config["expect_long_running"],
                "policy": config["policy"],
                "max_concurrent": config["max_concurrent"],
                "max_no_progress_seconds": config["max_no_progress_seconds"],
                "soft_duration_seconds": config["soft_duration_seconds"],
                "soft_output_bytes": config["soft_output_bytes"],
                "soft_token_budget": config["soft_token_budget"],
                "usage_adapter": config["usage_adapter"],
                "policy_files": (
                    [str(path) for path in config["policy_config"]["files"]]
                    if config["policy_config"] is not None
                    else []
                ),
                "policy_metadata": (
                    config["policy_config"]["metadata"]
                    if config["policy_config"] is not None
                    else {}
                ),
                "bundled_policy": config["bundled_policy"],
                "availability_probe_configured": (
                    config["availability_probe"] is not None
                ),
                "availability_timeout_seconds": config["availability_timeout_seconds"],
                "admission": config["admission"],
                "warnings": config["warnings"],
                **config["extras"],
            }
            for name, config in sorted(registry.items())
        },
    }


def diagnose_workers(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    worker: str | None = None,
    minimum_severity: str = "info",
    enabled_only: bool = False,
) -> dict[str, Any]:
    registry = load_registry(project_root, state_dir=state_dir)
    dispatch = load_dispatch_config(project_root, state_dir=state_dir)
    if worker is not None:
        if worker not in registry:
            configured = ", ".join(sorted(registry)) or "<none>"
            raise WorkerError(f"unknown worker: {worker}; configured: {configured}")
        registry = {worker: registry[worker]}
    if enabled_only:
        registry = {
            name: config for name, config in registry.items() if config["enabled"]
        }

    summaries: dict[str, Any] = {}
    all_diagnostics: list[dict[str, str]] = []
    for name, config in sorted(registry.items()):
        profile_diagnostics = list(config["diagnostics"])
        admission = config.get("admission")
        if (
            dispatch["intent_enforcement"] == "strict"
            and worker_diagnostics.is_known_ai_profile(config["command"])
            and (
                not isinstance(admission, dict)
                or not admission.get("verification")
            )
        ):
            profile_diagnostics.append(
                worker_diagnostics.diagnostic(
                    code="strict_ai_verification_not_declared",
                    severity="warning",
                    message=(
                        f"worker {name} is an AI profile under strict intent "
                        "enforcement but declares no supported verification levels"
                    ),
                    suggested_action=(
                        "Add workers.NAME.admission.verification and dispatch AI "
                        "tasks with an explicit intent.verification value."
                    ),
                )
            )
        diagnostics = worker_diagnostics.filter_diagnostics(
            profile_diagnostics,
            minimum_severity=minimum_severity,
        )
        all_diagnostics.extend(diagnostics)
        summaries[name] = worker_diagnostics.profile_summary(
            name=name,
            config=config,
            diagnostics=diagnostics,
        )

    policy_summaries: dict[str, Any] = {}
    policy_diagnostics: list[dict[str, str]] = []
    for config in registry.values():
        bundled = config.get("bundled_policy")
        if not isinstance(bundled, dict):
            continue
        policy_name = str(bundled["name"])
        policy_summaries.setdefault(policy_name, bundled)
    for policy_name, bundled in sorted(policy_summaries.items()):
        if bundled.get("status") != "different":
            continue
        item = worker_diagnostics.diagnostic(
            code="policy_update_available",
            severity="info",
            message=(
                f"project policy {policy_name} differs from bundled revision "
                f"{bundled['revision']}"
            ),
            suggested_action=(
                "Compare the local policy with the bundled reference. Keep "
                "intentional customizations or update the local copy explicitly; "
                "OrchestratorEngine never overwrites it automatically."
            ),
        )
        filtered = worker_diagnostics.filter_diagnostics(
            [item], minimum_severity=minimum_severity
        )
        policy_diagnostics.extend(filtered)
        all_diagnostics.extend(filtered)

    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_DIAGNOSTICS",
        "config_path": str(workers_config_path(project_root, state_dir=state_dir)),
        "dispatch": dispatch,
        "filters": {
            "worker": worker,
            "minimum_severity": minimum_severity,
            "enabled_only": enabled_only,
        },
        "worker_count": len(summaries),
        "diagnostic_count": len(all_diagnostics),
        "severity_counts": worker_diagnostics.severity_counts(all_diagnostics),
        "worst_severity": worker_diagnostics.worst_severity(all_diagnostics),
        "policies": policy_summaries,
        "policy_diagnostics": policy_diagnostics,
        "workers": summaries,
    }


def availability_workers(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    worker: str | None = None,
    enabled_only: bool = True,
) -> dict[str, Any]:
    registry = load_registry(project_root, state_dir=state_dir)
    if worker is not None:
        if worker not in registry:
            raise WorkerError(f"unknown worker: {worker}")
        registry = {worker: registry[worker]}
    results = {}
    for name, config in sorted(registry.items()):
        if enabled_only and not config["enabled"]:
            continue
        results[name] = worker_diagnostics.run_availability_probe(config)
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_AVAILABILITY",
        "worker_count": len(results),
        "workers": results,
    }


def queue_root(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "queue"


@contextlib.contextmanager
def admission_lock(project_root: Path, *, state_dir: str):
    path = queue_root(project_root, state_dir=state_dir) / "admission.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def active_task_counts(
    project_root: Path,
    *,
    state_dir: str,
) -> tuple[int, dict[str, int]]:
    total = 0
    by_worker: dict[str, int] = {}
    for path in sorted(
        tasks_root(project_root, state_dir=state_dir).glob("*/task.json")
    ):
        with contextlib.suppress(OSError, core.OrchestratorError):
            descriptor = core.load_object(path)
            if descriptor.get("status") not in {"starting", "running", "cancelling"}:
                continue
            worker = descriptor.get("worker")
            if not isinstance(worker, str):
                continue
            total += 1
            by_worker[worker] = by_worker.get(worker, 0) + 1
    return total, by_worker


def can_admit_worker(
    project_root: Path,
    *,
    worker: str,
    config: dict[str, Any],
    state_dir: str,
) -> bool:
    total, by_worker = active_task_counts(project_root, state_dir=state_dir)
    global_limit = load_dispatch_config(project_root, state_dir=state_dir)[
        "max_concurrent"
    ]
    worker_limit = config.get("max_concurrent")
    return not (
        (global_limit is not None and total >= global_limit)
        or (worker_limit is not None and by_worker.get(worker, 0) >= worker_limit)
    )


def supervisor_command(
    project_root: Path,
    *,
    worker: str,
    task_id: str,
    prompt_file: Path,
    state_dir: str,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "orchestrator_engine.cli",
        "--project-root",
        str(project_root),
        "--state-dir",
        state_dir,
        "worker",
        "supervise",
        "--worker",
        worker,
        "--task-id",
        task_id,
        "--prompt-file",
        str(prompt_file),
    ]


def spawn_supervisor(
    project_root: Path,
    *,
    worker: str,
    task_id: str,
    prompt_file: Path,
    state_dir: str,
    supervisor_log: Path,
    popen_factory=subprocess.Popen,
) -> Any:
    with supervisor_log.open("ab") as log:
        process = popen_factory(
            supervisor_command(
                project_root,
                worker=worker,
                task_id=task_id,
                prompt_file=prompt_file,
                state_dir=state_dir,
            ),
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    if hasattr(process, "poll"):
        remember_detached_process(process)
    return process


def pending_queue_path(project_root: Path, task_id: str, *, state_dir: str) -> Path:
    return queue_root(project_root, state_dir=state_dir) / "pending" / f"{task_id}.json"


def fingerprint_root(project_root: Path, *, state_dir: str) -> Path:
    return (
        core.state_root(project_root, state_dir=state_dir) / "dispatch" / "fingerprints"
    )


def claim_dispatch_fingerprint(
    project_root: Path,
    *,
    descriptor: dict[str, Any],
    config: dict[str, Any],
    intent_sha256: str | None,
    state_dir: str,
    allow_duplicate: bool,
    duplicate_reason: str | None,
) -> dict[str, Any]:
    policy = descriptor.get("worker_policy")
    policy_identity = (
        {key: policy.get(key) for key in ("name", "files", "metadata")}
        if isinstance(policy, dict)
        else None
    )
    fingerprint = canonical_sha256(
        {
            "version": 1,
            "worker": descriptor["worker"],
            "prompt_sha256": descriptor["prompt_sha256"],
            "worker_policy": policy_identity,
            "intent_sha256": intent_sha256,
            "command": config["command"],
        }
    )
    descriptor["dispatch_fingerprint"] = fingerprint
    claim_dir = fingerprint_root(project_root, state_dir=state_dir) / fingerprint
    claim_path = claim_dir / f"{descriptor['task_id']}.json"
    claim = {
        "schema_version": 1,
        "kind": "WORKER_DISPATCH_CLAIM",
        "fingerprint": fingerprint,
        "task_id": descriptor["task_id"],
        "worker": descriptor["worker"],
        "created_at": core.utc_now(),
    }
    active_tasks: list[str] = []
    for existing_claim_path in sorted(claim_dir.glob("*.json")):
        with contextlib.suppress(OSError, core.OrchestratorError):
            existing = core.load_object(existing_claim_path)
            existing_task = str(existing.get("task_id", ""))
            existing_path = (
                task_dir_for(project_root, existing_task, state_dir=state_dir)
                / "task.json"
            )
            existing_descriptor = core.load_object(existing_path)
            if existing_descriptor.get("status") in core.TERMINAL_STATUSES:
                release_dispatch_claim(
                    project_root,
                    existing_descriptor,
                    state_dir=state_dir,
                )
            else:
                active_tasks.append(existing_task)
    if active_tasks and not allow_duplicate:
        raise WorkerError(
            f"exact duplicate of active task {active_tasks[0]}; "
            "use --allow-duplicate with --duplicate-reason to override"
        )
    if active_tasks and (not duplicate_reason or not duplicate_reason.strip()):
        raise WorkerError("--allow-duplicate requires --duplicate-reason")
    if not core.claim_json(claim_path, claim):
        raise WorkerError(
            f"dispatch claim already exists for task {descriptor['task_id']}"
        )
    descriptor["dispatch_claim_path"] = str(claim_path)
    if active_tasks:
        descriptor["duplicate_override"] = {
            "reason": duplicate_reason.strip(),
            "conflicts_with_task_id": active_tasks[0],
            "active_conflicts": active_tasks,
            "recorded_at": core.utc_now(),
        }
    return descriptor


def release_dispatch_claim(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
) -> None:
    value = descriptor.get("dispatch_claim_path")
    if not isinstance(value, str):
        return
    claim_path = Path(value)
    if not claim_path.is_file():
        return
    history = (
        core.state_root(project_root, state_dir=state_dir)
        / "dispatch"
        / "history"
        / f"{descriptor.get('task_id', claim_path.stem)}.json"
    )
    history.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(claim_path, history)
    except FileNotFoundError:
        return


def enqueue_task(
    project_root: Path,
    descriptor: dict[str, Any],
    *,
    state_dir: str,
) -> dict[str, Any]:
    queued = {**descriptor, "status": "queued", "queued_at": core.utc_now()}
    entry = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_QUEUE_ENTRY",
        "task_id": queued["task_id"],
        "worker": queued["worker"],
        "created_at": queued["created_at"],
        "queued_at": queued["queued_at"],
        "descriptor_path": str(Path(queued["task_dir"]) / "task.json"),
    }
    core.atomic_json(Path(entry["descriptor_path"]), queued)
    core.atomic_json(
        pending_queue_path(project_root, queued["task_id"], state_dir=state_dir),
        entry,
    )
    return queued


def task_not_before_pending(descriptor: dict[str, Any]) -> bool:
    lineage = descriptor.get("retry_lineage")
    value = lineage.get("not_before") if isinstance(lineage, dict) else None
    if not isinstance(value, str):
        return False
    try:
        not_before = datetime.fromisoformat(value)
    except ValueError:
        return False
    if not_before.tzinfo is None:
        not_before = not_before.replace(tzinfo=UTC)
    return datetime.now(UTC) < not_before.astimezone(UTC)


def queue_tick(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    popen_factory=subprocess.Popen,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    with contextlib.suppress(OSError, core.OrchestratorError, WorkerError):
        reap_worker_tasks(project, state_dir=state_dir)
    admitted: list[str] = []
    with admission_lock(project, state_dir=state_dir):
        pending = queue_root(project, state_dir=state_dir) / "pending"
        entries: list[tuple[str, str, Path, dict[str, Any]]] = []
        for path in pending.glob("*.json"):
            with contextlib.suppress(OSError, core.OrchestratorError):
                entry = core.load_object(path)
                entries.append(
                    (
                        str(entry.get("queued_at", "")),
                        str(entry.get("task_id", "")),
                        path,
                        entry,
                    )
                )
        for _, task_id, path, entry in sorted(entries):
            descriptor_path = Path(str(entry.get("descriptor_path", "")))
            try:
                descriptor = core.load_object(descriptor_path)
                config = require_worker(
                    project, str(entry["worker"]), state_dir=state_dir
                )
            except (OSError, KeyError, core.OrchestratorError, WorkerError):
                continue
            if descriptor.get("status") != "queued":
                path.unlink(missing_ok=True)
                continue
            if task_not_before_pending(descriptor) or not can_admit_worker(
                project,
                worker=str(entry["worker"]),
                config=config,
                state_dir=state_dir,
            ):
                continue
            claimed = queue_root(project, state_dir=state_dir) / "admitted" / path.name
            claimed.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, claimed)
            except FileNotFoundError:
                continue
            descriptor["status"] = "starting"
            descriptor["admitted_at"] = core.utc_now()
            descriptor["queue_entry_path"] = str(claimed)
            core.atomic_json(descriptor_path, descriptor)
            try:
                process = spawn_supervisor(
                    project,
                    worker=str(entry["worker"]),
                    task_id=task_id,
                    prompt_file=Path(str(descriptor["prompt_file"])),
                    state_dir=state_dir,
                    supervisor_log=Path(str(descriptor["supervisor_log"])),
                    popen_factory=popen_factory,
                )
            except OSError as error:
                descriptor["status"] = "queued"
                descriptor["queue_last_error"] = str(error)
                descriptor.pop("admitted_at", None)
                core.atomic_json(descriptor_path, descriptor)
                os.replace(claimed, path)
                continue
            admitted.append(task_id)
            entry["supervisor_pid"] = int(process.pid)
            entry["admitted_at"] = descriptor["admitted_at"]
            core.atomic_json(claimed, entry)
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_QUEUE_TICK",
        "admitted_count": len(admitted),
        "admitted_task_ids": admitted,
    }


def run_worker(
    project_root: Path,
    *,
    worker: str,
    task_id: str,
    prompt_file: Path,
    state_dir: str = core.DEFAULT_STATE_DIR,
    popen_factory=subprocess.Popen,
    preflight_availability: bool = False,
    availability_mode: str | None = None,
    intent_file: Path | None = None,
    allow_duplicate: bool = False,
    duplicate_reason: str | None = None,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spawn a detached supervisor for the worker and return immediately.

    `task.json` has a single writer at a time and ownership is handed off at the
    spawn: the dispatcher writes the descriptor before the supervisor exists and
    never writes it again. A post-spawn read-modify-write here would race the
    supervisor's own writes and could resurrect a finished task as `running`.
    """
    project = project_root.expanduser().resolve()
    config = require_worker(project, worker, state_dir=state_dir)
    if preflight_availability and availability_mode is not None:
        raise WorkerError(
            "--preflight-availability cannot be combined with --availability-mode"
        )
    dispatch = load_dispatch_config(project, state_dir=state_dir)
    prompt = prompt_file.expanduser().resolve()
    intent, intent_sha256 = load_task_intent(intent_file)
    intent_admission = enforce_task_intent(
        project,
        intent=intent,
        config=config,
        state_dir=state_dir,
    )
    effective_availability_mode = (
        "block-unavailable"
        if preflight_availability
        else availability_mode or dispatch["availability_mode"]
    )
    if effective_availability_mode not in AVAILABILITY_MODES:
        raise WorkerError(
            "availability mode must be one of: "
            + ", ".join(sorted(AVAILABILITY_MODES))
        )
    availability_snapshot: dict[str, Any] | None = None
    if effective_availability_mode != "off":
        availability_snapshot = {
            "mode": effective_availability_mode,
            "checked_at": core.utc_now(),
            **worker_diagnostics.run_availability_probe(config),
        }
        status = availability_snapshot["status"]
        blocked = status == "unavailable" or (
            effective_availability_mode == "require-available" and status != "available"
        )
        if blocked:
            raise WorkerError(
                f"worker {worker} availability preflight {status} "
                f"under mode {effective_availability_mode}"
            )
    task_dir = task_dir_for(project, task_id, state_dir=state_dir)
    descriptor_path = task_dir / "task.json"
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive create claims the task id atomically (no check-then-act
        # race between concurrent dispatches).
        with descriptor_path.open("x", encoding="utf-8") as handle:
            handle.write("{}\n")
    except FileExistsError:
        raise WorkerError(f"task already exists: {descriptor_path}") from None
    supervisor_log = task_dir / "supervisor.log"
    try:
        prompt_snapshot = worker_policy.snapshot_prompt(
            project,
            prompt_file=prompt,
            task_dir=task_dir,
            policy=config["policy_config"],
            intent=intent,
        )
        handoff_path = task_dir / "worker-handoff.json"
        output_dir = task_dir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        effective_path = Path(prompt_snapshot["effective_prompt_file"])
        effective_text = effective_path.read_text(encoding="utf-8")
        worker_policy.atomic_text(
            effective_path,
            effective_text
            + "\nORCHESTRATOR_OPTIONAL_HANDOFF v1\n"
            + f"path: {handoff_path}\n"
            + "You may write one compact UTF-8 JSON object at that path. "
            + "evidence, risks and next_actions are arrays when present.\n"
            + "BEGIN_HANDOFF_EXAMPLE\n"
            + '{"schema_version":1,"kind":"WORKER_HANDOFF",'
            + '"summary":"Concise completed-work summary",'
            + '"evidence":[],"risks":[],"next_actions":[]}\n'
            + "END_HANDOFF_EXAMPLE\n"
            + "The handoff is evidence only and never controls the orchestrator.\n"
            + "ORCHESTRATOR_DURABLE_OUTPUT v1\n"
            + f"directory: {output_dir}\n"
            + "The complete requested deliverable must be present in stdout or "
            + "as one or more files below this directory. A provider-owned plan "
            + "or cache file outside the task directory is not durable evidence; "
            + "do not return only a pointer to it.\n",
        )
        prompt_snapshot["effective_prompt_sha256"] = core.sha256_file(effective_path)
        prompt_snapshot["handoff_path"] = str(handoff_path)
        prompt_snapshot["declared_output_dir"] = str(output_dir)
    except worker_policy.WorkerPolicyError as error:
        descriptor_path.unlink(missing_ok=True)
        raise WorkerError(str(error)) from error
    # Snapshot the dispatching chat BEFORE spawning: the supervisor reads
    # wake_target from task.json, so it must be durable before the child can
    # possibly look for it.
    wake_target = capture_wake_target(project, state_dir=state_dir)
    descriptor = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": TASK_KIND,
        "task_id": task_id,
        "worker": worker,
        "status": "queued",
        "prompt_file": str(prompt),
        **prompt_snapshot,
        "task_dir": str(task_dir),
        "supervisor_log": str(supervisor_log),
        "created_at": core.utc_now(),
        "runtime_policy": {
            key: config.get(key)
            for key in (
                "max_no_progress_seconds",
                "soft_duration_seconds",
                "soft_output_bytes",
                "soft_token_budget",
                "usage_adapter",
            )
            if config.get(key) is not None
        },
        "lease_required": True,
    }
    if intent is not None:
        intent_path = task_dir / "intent.json"
        core.atomic_json(intent_path, intent)
        descriptor["intent_file"] = str(intent_path)
        descriptor["intent_sha256"] = intent_sha256
        descriptor["task_intent"] = intent
    if intent_admission is not None:
        descriptor["intent_admission"] = intent_admission
    if availability_snapshot is not None:
        descriptor["availability_preflight"] = availability_snapshot
    if lineage is not None:
        descriptor["retry_lineage"] = lineage
    if config["warnings"]:
        descriptor["warnings"] = config["warnings"]
    if wake_target is not None:
        descriptor["wake_target"] = wake_target
    try:
        with admission_lock(project, state_dir=state_dir):
            descriptor = claim_dispatch_fingerprint(
                project,
                descriptor=descriptor,
                config=config,
                intent_sha256=intent_sha256,
                state_dir=state_dir,
                allow_duplicate=allow_duplicate,
                duplicate_reason=duplicate_reason,
            )
            core.atomic_json(descriptor_path, descriptor)
            if task_not_before_pending(descriptor) or not can_admit_worker(
                project,
                worker=worker,
                config=config,
                state_dir=state_dir,
            ):
                queued = enqueue_task(project, descriptor, state_dir=state_dir)
                return {**queued, "descriptor_path": str(descriptor_path)}
            descriptor["status"] = "starting"
            core.atomic_json(descriptor_path, descriptor)
            process = spawn_supervisor(
                project,
                worker=worker,
                task_id=task_id,
                prompt_file=prompt,
                state_dir=state_dir,
                supervisor_log=supervisor_log,
                popen_factory=popen_factory,
            )
    except WorkerError:
        descriptor_path.unlink(missing_ok=True)
        raise
    # The task stays `starting` on disk until the supervisor claims it and
    # records its own pid: the dispatcher must not write the descriptor again.
    # The spawned pid is still reported to the dispatching chat.
    return {
        **descriptor,
        "supervisor_pid": int(process.pid),
        "descriptor_path": str(descriptor_path),
    }


def capture_wake_target(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any] | None:
    bound = binding.load_binding(project_root, state_dir=state_dir)
    if bound is None:
        return None
    return binding.wake_target_from_binding(bound)


def worker_process_group(pid: int) -> int | None:
    """Return the process group the worker leads, or None if it leads none.

    The supervisor only signals a group whose leader is the worker itself. A
    worker that shares the supervisor's group (an injected `popen_factory` that
    ignores `process_group`) must be signalled as a single process, because
    signalling that group would kill the supervisor too.
    """
    try:
        group = os.getpgid(pid)
    except OSError:
        return None
    if group != pid or group == os.getpgid(0):
        return None
    return group


def wait_for_exit(pid: int, *, timeout_seconds: float, poll_seconds: float) -> bool:
    """Wait for a child to exit without reaping it.

    `WNOWAIT` leaves the exited child as a zombie, which keeps its pid — and so
    its process group id — reserved. That is what makes it safe to send the
    group a final signal after the worker itself has already died.
    """
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        try:
            state = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return True
        if state is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_seconds)


def terminate_worker(
    process: Any,
    *,
    process_group: int | None,
    reason: str,
    grace_seconds: float = WORKER_TERMINATION_GRACE_SECONDS,
    timeout_seconds: float = WORKER_TERMINATION_TIMEOUT_SECONDS,
    poll_seconds: float = WORKER_TERMINATION_POLL_SECONDS,
) -> dict[str, Any]:
    """Stop the worker and its descendants, and return the signal ledger.

    Termination is group-wide and escalates deterministically: SIGTERM so the
    worker can flush and exit, then a bounded grace period, then SIGKILL. Killing
    only the direct child would leave the model CLI's own subprocesses running as
    orphans that keep writing into the task's inherited log files after the
    result is durable.
    """
    scope = "process_group" if process_group is not None else "process"
    signals: list[dict[str, str]] = []

    def deliver(sent: signal.Signals) -> None:
        try:
            if process_group is not None:
                os.killpg(process_group, sent)
            else:
                os.kill(process.pid, sent)
        except OSError:
            # The target is already gone; nothing to escalate against.
            return
        signals.append({"signal": sent.name, "scope": scope, "at": core.utc_now()})

    deliver(signal.SIGTERM)
    exited = wait_for_exit(
        process.pid,
        timeout_seconds=grace_seconds,
        poll_seconds=poll_seconds,
    )
    escalated = not exited
    if escalated or process_group is not None:
        # Forced escalation for a worker that ignored SIGTERM, and — when the
        # worker did stop — a sweep for descendants that did not.
        deliver(signal.SIGKILL)
    if escalated:
        exited = wait_for_exit(
            process.pid,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=timeout_seconds)
    return {
        "reason": reason,
        "scope": scope,
        "process_group": process_group,
        "grace_seconds": grace_seconds,
        "escalated": escalated,
        "exited": exited,
        "signals": signals,
    }


def force_terminate_worker(
    process: Any, *, process_group: int | None
) -> dict[str, Any]:
    scope = "process_group" if process_group is not None else "process"
    sent = False
    try:
        if process_group is not None:
            os.killpg(process_group, signal.SIGKILL)
        else:
            os.kill(process.pid, signal.SIGKILL)
        sent = True
    except OSError:
        pass
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=WORKER_TERMINATION_TIMEOUT_SECONDS)
    return {
        "reason": "cancelled_forced",
        "scope": scope,
        "process_group": process_group,
        "grace_seconds": 0.0,
        "escalated": True,
        "exited": process.poll() is not None,
        "signals": (
            [{"signal": "SIGKILL", "scope": scope, "at": core.utc_now()}]
            if sent
            else []
        ),
    }


def cancel_request_path(task_dir: Path) -> Path:
    return task_dir / "control" / "cancel.json"


def load_cancel_request(task_dir: Path) -> dict[str, Any] | None:
    path = cancel_request_path(task_dir)
    if not path.is_file():
        return None
    with contextlib.suppress(OSError, core.OrchestratorError):
        request = core.load_object(path)
        if request.get("kind") == "WORKER_CANCEL_REQUEST":
            return request
    return None


def queued_cancellation_artifacts(
    descriptor: dict[str, Any],
    *,
    reason: str,
    mode: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now = core.utc_now()
    task_dir = Path(str(descriptor["task_dir"]))
    result = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_RESULT",
        "task_id": descriptor["task_id"],
        "worker": descriptor["worker"],
        "terminal_status": "cancelled",
        "exit_code": None,
        "failure_reason": reason,
        "duration_seconds": 0.0,
        "stdout_path": str(task_dir / "worker-stdout.log"),
        "stderr_path": str(task_dir / "worker-stderr.log"),
        "started_at": descriptor["created_at"],
        "finished_at": now,
        "cancellation": {"mode": mode, "reason": reason, "before_start": True},
    }
    evidence = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_EVIDENCE",
        "task_id": descriptor["task_id"],
        "worker": descriptor["worker"],
        "command": ["<not started: cancelled while queued>"],
        "prompt_file": descriptor["prompt_file"],
        "prompt_sha256": descriptor.get("prompt_sha256"),
        "worker_config": {"cancelled_before_start": True},
        "started_at": descriptor["created_at"],
        "finished_at": now,
        "cancellation": result["cancellation"],
    }
    for key in ("effective_prompt_file", "effective_prompt_sha256", "worker_policy"):
        if key in descriptor:
            evidence[key] = descriptor[key]
    if isinstance(descriptor.get("wake_target"), dict):
        evidence["wake_target"] = descriptor["wake_target"]
    return result, evidence


def cancel_worker_task(
    project_root: Path,
    *,
    task_id: str,
    mode: str,
    reason: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    if mode not in {"graceful", "forced"}:
        raise WorkerError(f"unsupported cancellation mode: {mode}")
    if not reason.strip():
        raise WorkerError("cancellation reason is required")
    project = project_root.expanduser().resolve()
    task_dir = task_dir_for(project, task_id, state_dir=state_dir)
    descriptor_path = task_dir / "task.json"
    descriptor = core.load_object(descriptor_path)
    status = descriptor.get("status")
    if status in core.TERMINAL_STATUSES:
        return {
            "schema_version": 1,
            "kind": "WORKER_CANCEL_RESULT",
            "task_id": task_id,
            "status": "already_terminal",
            "terminal_status": status,
        }
    if status == "queued":
        with admission_lock(project, state_dir=state_dir):
            descriptor = core.load_object(descriptor_path)
            if descriptor.get("status") != "queued":
                status = descriptor.get("status")
            else:
                pending = pending_queue_path(project, task_id, state_dir=state_dir)
                cancelled_entry = (
                    queue_root(project, state_dir=state_dir)
                    / "cancelled"
                    / f"{task_id}.json"
                )
                cancelled_entry.parent.mkdir(parents=True, exist_ok=True)
                if pending.exists():
                    os.replace(pending, cancelled_entry)
                result, evidence = queued_cancellation_artifacts(
                    descriptor, reason=reason, mode=mode
                )
                finalized = finalize_terminal_task(
                    project,
                    task_id=task_id,
                    task_dir=task_dir,
                    result=result,
                    evidence=evidence,
                    state_dir=state_dir,
                    wake_target=(
                        descriptor.get("wake_target")
                        if isinstance(descriptor.get("wake_target"), dict)
                        else None
                    ),
                )
                descriptor.update(
                    {
                        "status": "cancelled",
                        "finished_at": result["finished_at"],
                        "event_path": finalized["event_path"],
                        "signal_path": finalized["signal_path"],
                    }
                )
                core.atomic_json(descriptor_path, descriptor)
                release_dispatch_claim(project, descriptor, state_dir=state_dir)
                return {
                    "schema_version": 1,
                    "kind": "WORKER_CANCEL_RESULT",
                    "task_id": task_id,
                    "status": "cancelled",
                    "before_start": True,
                }
    request = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_CANCEL_REQUEST",
        "task_id": task_id,
        "mode": mode,
        "reason": reason.strip(),
        "requested_at": core.utc_now(),
    }
    path = cancel_request_path(task_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not core.claim_json(path, request):
        request = core.load_object(path)
        return {
            "schema_version": 1,
            "kind": "WORKER_CANCEL_RESULT",
            "task_id": task_id,
            "status": "already_requested",
            "request": request,
        }
    return {
        "schema_version": 1,
        "kind": "WORKER_CANCEL_RESULT",
        "task_id": task_id,
        "status": "requested",
        "request_path": str(path),
        "observed_task_status": status,
    }


def retry_worker_task(
    project_root: Path,
    *,
    task_id: str,
    reason: str,
    new_task_id: str | None = None,
    max_attempts: int = 3,
    delay_seconds: float = 0.0,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    if not reason.strip():
        raise WorkerError("retry reason is required")
    if max_attempts < 2:
        raise WorkerError("max_attempts must be at least 2")
    if not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise WorkerError("delay_seconds must be finite and non-negative")
    project = project_root.expanduser().resolve()
    parent_path = task_dir_for(project, task_id, state_dir=state_dir) / "task.json"
    parent = core.load_object(parent_path)
    if parent.get("status") not in {
        "failed",
        "timed_out",
        "rate_limited",
        "invalid_result",
    }:
        raise WorkerError("only unsuccessful terminal tasks may be retried")
    previous = parent.get("retry_lineage")
    attempt = int(previous.get("attempt", 1)) + 1 if isinstance(previous, dict) else 2
    inherited_max = (
        int(previous.get("max_attempts", max_attempts))
        if isinstance(previous, dict)
        else max_attempts
    )
    effective_max = min(max_attempts, inherited_max)
    if attempt > effective_max:
        raise WorkerError(
            f"retry attempt {attempt} exceeds max_attempts {effective_max}"
        )
    root_task_id = (
        str(previous.get("root_task_id"))
        if isinstance(previous, dict) and previous.get("root_task_id")
        else task_id
    )
    retry_id = new_task_id or f"{root_task_id}-a{attempt}"
    task_prompt = parent.get("task_prompt_file") or parent.get("prompt_file")
    if not isinstance(task_prompt, str) or not Path(task_prompt).is_file():
        raise WorkerError("retry source task prompt is unavailable")
    intent_path = parent.get("intent_file")
    lineage = {
        "schema_version": 1,
        "root_task_id": root_task_id,
        "parent_task_id": task_id,
        "attempt": attempt,
        "max_attempts": effective_max,
        "attempt_reason": reason.strip(),
        "created_at": core.utc_now(),
        "not_before": (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat(
            timespec="milliseconds"
        ),
    }
    return run_worker(
        project,
        worker=str(parent["worker"]),
        task_id=retry_id,
        prompt_file=Path(task_prompt),
        state_dir=state_dir,
        intent_file=Path(intent_path) if isinstance(intent_path, str) else None,
        lineage=lineage,
    )


def load_terminal_result(result_path: Path) -> dict[str, Any] | None:
    """Return an existing terminal result, or None if it is absent or torn.

    A zero-length or unparsable `result.json` is a claim whose holder died
    mid-write. It carries no evidence, so it is not a terminal record — but only
    a caller that has proven the claim holder is dead may replace it.
    """
    try:
        result = core.load_object(result_path)
    except (OSError, core.OrchestratorError):
        return None
    if result.get("kind") != "WORKER_RESULT":
        return None
    if result.get("terminal_status") not in core.TERMINAL_STATUSES:
        return None
    return result


def worker_wait_snapshot(
    project_root: Path,
    *,
    task_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    stale_after_seconds: float = TASK_HEARTBEAT_INTERVAL_SECONDS * 3,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the bounded state needed by a human or deterministic waiter."""
    project = project_root.expanduser().resolve()
    task_dir = task_dir_for(project, task_id, state_dir=state_dir)
    descriptor_path = task_dir / "task.json"
    descriptor = core.load_object(descriptor_path)
    result_path = task_dir / "result.json"
    result = load_terminal_result(result_path)
    descriptor_status = str(descriptor.get("status") or "unknown")
    result_status = (
        str(result["terminal_status"]) if result is not None else None
    )
    terminal = result is not None and descriptor_status in core.TERMINAL_STATUSES
    status = (
        result_status
        if terminal
        else ("finalizing" if result is not None else descriptor_status)
    )
    snapshot: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_WAIT_STATUS",
        "task_id": task_id,
        "worker": descriptor.get("worker")
        or (result.get("worker") if result is not None else None),
        "status": status,
        "terminal": terminal,
        "task_path": str(descriptor_path),
    }
    progress = descriptor.get("progress")
    if isinstance(progress, dict):
        snapshot["progress"] = {
            key: progress.get(key)
            for key in (
                "heartbeat_count",
                "stdout_bytes",
                "stderr_bytes",
                "last_output_growth_at",
                "updated_at",
            )
            if progress.get(key) is not None
        }
    if result is not None:
        failure_reason = result.get("failure_reason")
        if isinstance(failure_reason, str):
            failure_reason = failure_reason[:500]
        snapshot.update(
            {
                "exit_code": result.get("exit_code"),
                "terminal_status": result_status,
                "failure_reason": failure_reason,
                "duration_seconds": result.get("duration_seconds"),
                "finished_at": result.get("finished_at"),
                "result_path": str(result_path),
            }
        )
        evidence_path = task_dir / "evidence.json"
        if evidence_path.is_file():
            snapshot["evidence_path"] = str(evidence_path)
    health = worker_wait_health(
        task_dir,
        descriptor=descriptor,
        descriptor_status=descriptor_status,
        result=result,
        stale_after_seconds=stale_after_seconds,
        now=now or datetime.now(UTC),
    )
    if health is not None:
        snapshot["health"] = health
    snapshot["suggested_action"] = wait_suggested_action(status)
    return snapshot


def worker_wait_health(
    task_dir: Path,
    *,
    descriptor: dict[str, Any],
    descriptor_status: str,
    result: dict[str, Any] | None,
    stale_after_seconds: float,
    now: datetime,
) -> dict[str, Any] | None:
    if result is None and descriptor_status in core.TERMINAL_STATUSES:
        return {
            "status": "terminal_result_unreadable",
            "message": "terminal task descriptor has no readable result",
        }
    if descriptor_status not in {"starting", "running", "cancelling"}:
        return None
    heartbeat_age = worker_lease.lease_age_seconds(
        {
            "renewed_at": descriptor.get("last_alive_at")
            or descriptor.get("created_at")
        },
        now=now,
    )
    try:
        lease = worker_lease.load_lease(worker_lease.lease_path(task_dir))
    except worker_lease.WorkerLeaseError as error:
        return {
            "status": "lease_unreadable",
            "message": str(error)[:500],
            "heartbeat_age_seconds": heartbeat_age,
        }
    supervisor_state = (
        worker_lease.identity_state(lease.get("supervisor_identity"))
        if lease is not None
        else worker_lease.pid_state(descriptor.get("supervisor_pid"))
    )
    if supervisor_state["state"] == "gone":
        return {
            "status": "supervisor_dead",
            "message": "the recorded supervisor process is no longer alive",
            "heartbeat_age_seconds": heartbeat_age,
        }
    if heartbeat_age is not None and heartbeat_age > stale_after_seconds:
        return {
            "status": "heartbeat_stale",
            "message": (
                f"task heartbeat age {heartbeat_age:.1f}s exceeds "
                f"{stale_after_seconds:.1f}s"
            ),
            "heartbeat_age_seconds": round(heartbeat_age, 3),
            "supervisor_state": supervisor_state["state"],
        }
    return None


def wait_suggested_action(status: str) -> str:
    if status == "completed":
        return "Return to the orchestrating chat to review the worker result."
    if status in core.TERMINAL_STATUSES:
        return "Return to the orchestrating chat to review the failure evidence."
    return "Keep this command open; it will update when the worker finishes."


def wait_for_worker_task(
    project_root: Path,
    *,
    task_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    interval_seconds: float = 2.0,
    timeout_seconds: float | None = None,
    stale_after_seconds: float = TASK_HEARTBEAT_INTERVAL_SECONDS * 3,
    on_update: Callable[[dict[str, Any]], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Block without AI polling until a task is terminal or the wait times out."""
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise WorkerError("wait interval must be finite and positive")
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise WorkerError("wait timeout must be finite and positive")
    if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
        raise WorkerError("wait stale threshold must be finite and positive")
    started = monotonic()
    deadline = started + timeout_seconds if timeout_seconds is not None else None
    while True:
        snapshot = worker_wait_snapshot(
            project_root,
            task_id=task_id,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
        )
        now = monotonic()
        snapshot["waited_seconds"] = round(max(now - started, 0.0), 3)
        if on_update is not None:
            on_update(snapshot)
        if snapshot["terminal"]:
            return snapshot
        if snapshot.get("health") is not None:
            snapshot["wait_status"] = "action_required"
            snapshot["suggested_action"] = (
                "Return to the orchestrating chat and inspect task diagnostics."
            )
            if on_update is not None:
                on_update(snapshot)
            return snapshot
        if deadline is not None and now >= deadline:
            snapshot["wait_status"] = "timed_out"
            snapshot["suggested_action"] = (
                "The worker is still active; re-run this command later."
            )
            if on_update is not None:
                on_update(snapshot)
            return snapshot
        sleep_seconds = interval_seconds
        if deadline is not None:
            sleep_seconds = min(sleep_seconds, max(deadline - now, 0.0))
        sleeper(sleep_seconds)


def worker_wait_group_snapshot(
    project_root: Path,
    *,
    task_ids: list[str],
    mode: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
    stale_after_seconds: float = TASK_HEARTBEAT_INTERVAL_SECONDS * 3,
) -> dict[str, Any]:
    """Return one bounded aggregate snapshot for a declared task set."""
    validate_worker_wait_group(task_ids, mode=mode)
    snapshots = [
        worker_wait_snapshot(
            project_root,
            task_id=task_id,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
        )
        for task_id in task_ids
    ]
    terminal = [snapshot for snapshot in snapshots if snapshot["terminal"]]
    unsuccessful = [
        snapshot for snapshot in terminal if snapshot["status"] != "completed"
    ]
    action_required = [
        snapshot for snapshot in snapshots if snapshot.get("health") is not None
    ]
    condition_met = bool(terminal) if mode == "any" else len(terminal) == len(snapshots)
    if action_required:
        status = "action_required"
        wait_status = "action_required"
        suggested_action = (
            "Return to the orchestrating chat and inspect unhealthy task diagnostics."
        )
    elif condition_met:
        status = "unsuccessful" if unsuccessful else "completed"
        wait_status = "condition_met"
        suggested_action = (
            "Return to the orchestrating chat to review the completed task set."
        )
    else:
        status = "waiting"
        wait_status = "waiting"
        suggested_action = "Keep this command open; it will update when ready."
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_WAIT_GROUP_STATUS",
        "mode": mode,
        "status": status,
        "wait_status": wait_status,
        "condition_met": condition_met,
        "terminal": condition_met,
        "task_count": len(snapshots),
        "terminal_count": len(terminal),
        "completed_count": sum(
            snapshot["status"] == "completed" for snapshot in terminal
        ),
        "unsuccessful_count": len(unsuccessful),
        "action_required_count": len(action_required),
        "active_count": sum(
            not snapshot["terminal"] and snapshot.get("health") is None
            for snapshot in snapshots
        ),
        "task_ids": task_ids,
        "terminal_task_ids": [snapshot["task_id"] for snapshot in terminal],
        "action_required_task_ids": [
            snapshot["task_id"] for snapshot in action_required
        ],
        "tasks": snapshots,
        "suggested_action": suggested_action,
    }


def wait_for_worker_tasks(
    project_root: Path,
    *,
    task_ids: list[str],
    mode: str = "all",
    state_dir: str = core.DEFAULT_STATE_DIR,
    interval_seconds: float = 2.0,
    timeout_seconds: float | None = None,
    stale_after_seconds: float = TASK_HEARTBEAT_INTERVAL_SECONDS * 3,
    on_update: Callable[[dict[str, Any]], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Block on a bounded task set without AI or sequential task polling."""
    validate_worker_wait_group(task_ids, mode=mode)
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise WorkerError("wait interval must be finite and positive")
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise WorkerError("wait timeout must be finite and positive")
    if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
        raise WorkerError("wait stale threshold must be finite and positive")

    started = monotonic()
    deadline = started + timeout_seconds if timeout_seconds is not None else None
    while True:
        snapshot = worker_wait_group_snapshot(
            project_root,
            task_ids=task_ids,
            mode=mode,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
        )
        now = monotonic()
        snapshot["waited_seconds"] = round(max(now - started, 0.0), 3)
        if on_update is not None:
            on_update(snapshot)
        if snapshot["condition_met"] or snapshot["wait_status"] == "action_required":
            return snapshot
        if deadline is not None and now >= deadline:
            snapshot["status"] = "waiting"
            snapshot["wait_status"] = "timed_out"
            snapshot["suggested_action"] = (
                "The task set is still active; re-run this command later."
            )
            if on_update is not None:
                on_update(snapshot)
            return snapshot
        sleep_seconds = interval_seconds
        if deadline is not None:
            sleep_seconds = min(sleep_seconds, max(deadline - now, 0.0))
        sleeper(sleep_seconds)


def validate_worker_wait_group(task_ids: list[str], *, mode: str) -> None:
    """Validate the bounded task set shared by snapshots and blocking waits."""
    if not task_ids:
        raise WorkerError("wait requires at least one task id")
    if len(task_ids) > MAX_WAIT_TASKS:
        raise WorkerError(f"wait supports at most {MAX_WAIT_TASKS} task ids")
    if len(set(task_ids)) != len(task_ids):
        raise WorkerError("wait task ids must be unique")
    if mode not in WAIT_MODES:
        raise WorkerError(f"wait mode must be one of: {', '.join(sorted(WAIT_MODES))}")


def finalize_terminal_task(
    project_root: Path,
    *,
    task_id: str,
    task_dir: Path,
    result: dict[str, Any],
    evidence: dict[str, Any],
    state_dir: str = core.DEFAULT_STATE_DIR,
    wake_target: dict[str, Any] | None = None,
    takeover: bool = False,
) -> dict[str, Any]:
    """Write the terminal artifacts for a task as its single elected writer.

    `result.json` is created exclusively, and creating it *is* the election: a
    supervisor and a reaper that both believe they own the same task cannot both
    write, so the task can never end with two divergent terminal records. The
    loser reports the winner's result instead of overwriting it.

    `takeover` may only be set by a caller that has proven the previous writer is
    dead. It permits two recoveries, neither of which discards durable evidence:
    replacing an empty claim left by a writer that died mid-write, and completing
    a finalization whose result was written but whose event never was.

    Outcomes: `claimed` (this caller wrote the result), `reconciled` (the
    previous writer's result stands and this caller completed the emission), or
    `lost` (another writer owns the terminal record).
    """
    result_path = task_dir / "result.json"
    evidence_path = task_dir / "evidence.json"
    outcome = "claimed"
    final = result
    if not core.claim_json(result_path, result):
        existing = load_terminal_result(result_path)
        if existing is not None:
            if not takeover:
                return {
                    "outcome": "lost",
                    "result": existing,
                    "result_path": str(result_path),
                }
            outcome = "reconciled"
            final = existing
        elif takeover and result_path.stat().st_size == 0:
            core.atomic_json(result_path, result)
        else:
            return {
                "outcome": "lost",
                "result": None,
                "result_path": str(result_path),
                "conflict": "result.json exists but is not a readable terminal result",
            }

    terminal_status = str(final["terminal_status"])
    if not evidence_path.is_file() or outcome == "claimed":
        evidence["finished_at"] = final.get("finished_at", evidence["finished_at"])
        core.atomic_json(evidence_path, evidence)
    emitted = core.write_terminal_event(
        project_root,
        task_id=task_id,
        terminal_status=terminal_status,
        result_path=result_path,
        evidence_path=evidence_path,
        state_dir=state_dir,
        event_id=core.terminal_event_id(project_root, task_id=task_id),
        wake_target=wake_target,
    )
    return {
        "outcome": outcome,
        "result": final,
        "terminal_status": terminal_status,
        "result_path": str(result_path),
        "evidence_path": str(evidence_path),
        "event_path": emitted["event_path"],
        "signal_path": emitted["signal_path"],
    }


def supervise_worker(
    project_root: Path,
    *,
    worker: str,
    task_id: str,
    prompt_file: Path,
    state_dir: str = core.DEFAULT_STATE_DIR,
    popen_factory=subprocess.Popen,
    heartbeat_interval_seconds: float = TASK_HEARTBEAT_INTERVAL_SECONDS,
    termination_grace_seconds: float = WORKER_TERMINATION_GRACE_SECONDS,
) -> dict[str, Any]:
    """Run the worker CLI to completion and emit the terminal event.

    The supervisor owns `task.json` from the moment it starts: the dispatcher
    hands the descriptor over at spawn and never writes it again. The
    supervisor's in-memory snapshot is therefore authoritative, and each update
    publishes that whole object instead of re-reading a file another writer
    could have changed underneath it.
    """
    project = project_root.expanduser().resolve()
    config = require_worker(project, worker, state_dir=state_dir)
    prompt = prompt_file.expanduser().resolve()
    task_dir = task_dir_for(project, task_id, state_dir=state_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    wake_target: dict[str, Any] | None = None
    descriptor_path = task_dir / "task.json"
    descriptor_snapshot: dict[str, Any] = {}
    if descriptor_path.exists():
        with contextlib.suppress(OSError, core.OrchestratorError, binding.BindingError):
            descriptor_snapshot = core.load_object(descriptor_path)
            maybe_target = descriptor_snapshot.get("wake_target")
            if isinstance(maybe_target, dict):
                binding.validate_wake_target(maybe_target)
                wake_target = maybe_target

    def write_descriptor(updates: dict[str, Any]) -> None:
        descriptor_snapshot.update(updates)
        core.atomic_json(descriptor_path, descriptor_snapshot)

    # Claim the descriptor first: the dispatcher leaves the task `starting` with
    # no pid, so this is the first record of which process drives the task.
    descriptor_snapshot.setdefault("prompt_file", str(prompt))
    descriptor_snapshot.setdefault("created_at", core.utc_now())
    lease = worker_lease.acquire_lease(
        task_dir,
        task_id=task_id,
        worker=worker,
        interval_seconds=heartbeat_interval_seconds,
    )
    write_descriptor(
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": TASK_KIND,
            "task_id": task_id,
            "worker": worker,
            "task_dir": str(task_dir),
            "status": "running",
            "supervisor_pid": os.getpid(),
            "lease_path": str(worker_lease.lease_path(task_dir)),
            "last_alive_at": core.utc_now(),
        }
    )
    try:
        effective_prompt, policy_snapshot = worker_policy.load_snapshotted_prompt(
            descriptor_snapshot,
            original_prompt=prompt,
        )
        policy_was_snapshotted = "worker_policy" in descriptor_snapshot
        if (
            not policy_was_snapshotted
            and effective_prompt == prompt
            and config["policy_config"] is not None
        ):
            created_snapshot = worker_policy.snapshot_prompt(
                project,
                prompt_file=prompt,
                task_dir=task_dir,
                policy=config["policy_config"],
            )
            effective_prompt = Path(created_snapshot["effective_prompt_file"])
            policy_snapshot = created_snapshot.get("worker_policy")
            write_descriptor(created_snapshot)
        effective_prompt = core.ensure_file(
            effective_prompt,
            field="effective prompt",
        )
    except worker_policy.WorkerPolicyError as error:
        raise WorkerError(str(error)) from error
    source_prompt_hash = descriptor_snapshot.get("prompt_sha256")
    if not isinstance(source_prompt_hash, str):
        prompt = core.ensure_file(prompt, field="prompt")
        source_prompt_hash = core.sha256_file(prompt)
    effective_prompt_hash = core.sha256_file(effective_prompt)
    # Publish the prompt identity now, not at exit: a reaper that has to finalize
    # this task after the supervisor dies can only produce real evidence from
    # what the descriptor already holds. Values claimed at dispatch win.
    prompt_identity = {
        key: value
        for key, value in {
            "prompt_sha256": source_prompt_hash,
            "effective_prompt_file": str(effective_prompt),
            "effective_prompt_sha256": effective_prompt_hash,
        }.items()
        if key not in descriptor_snapshot
    }
    if prompt_identity:
        write_descriptor(prompt_identity)
    prompt_text = effective_prompt.read_text(encoding="utf-8")
    stdout_path = task_dir / "worker-stdout.log"
    stderr_path = task_dir / "worker-stderr.log"
    declared_output_dir = task_dir / "outputs"
    declared_output_dir.mkdir(parents=True, exist_ok=True)

    command = list(config["command"])
    stdin_payload: str | None = None
    if config["prompt_via"] == "arg":
        command.append(prompt_text)
    else:
        stdin_payload = prompt_text

    started_at = core.utc_now()
    start = time.monotonic()
    last_output_bytes = 0
    last_output_growth_at = started_at
    heartbeat_count = 0
    terminal_status = "completed"
    exit_code: int | None = None
    failure_reason: str | None = None
    termination: dict[str, Any] | None = None
    cancellation: dict[str, Any] | None = None
    try:
        with (
            stdout_path.open("wb") as stdout,
            stderr_path.open("wb") as stderr,
        ):
            process = popen_factory(
                command,
                cwd=str(project),
                env={
                    **os.environ,
                    "ORCHESTRATOR_TASK_DIR": str(task_dir),
                    "ORCHESTRATOR_HANDOFF_PATH": str(task_dir / "worker-handoff.json"),
                    "ORCHESTRATOR_DECLARED_OUTPUT_DIR": str(declared_output_dir),
                },
                stdin=subprocess.PIPE
                if stdin_payload is not None
                else (subprocess.DEVNULL),
                stdout=stdout,
                stderr=stderr,
                # The worker leads its own process group so the supervisor can
                # stop the whole worker tree — the model CLI's own subprocesses
                # included — without signalling itself.
                process_group=0,
            )
            # Record the group before any wait: a supervisor that dies here must
            # still leave behind the identity needed to stop the worker tree.
            worker_group = worker_process_group(process.pid)
            worker_identity: dict[str, Any] = {"worker_pid": process.pid}
            if worker_group is not None:
                worker_identity["worker_pgid"] = worker_group
            write_descriptor(worker_identity)
            lease = worker_lease.record_worker_identity(
                lease,
                task_dir,
                worker_pid=process.pid,
                worker_pgid=worker_group,
            )
            if stdin_payload is not None:
                assert process.stdin is not None
                # A very fast worker may exit before consuming stdin. That is
                # still its real process result, not a supervisor I/O failure.
                with contextlib.suppress(BrokenPipeError):
                    process.stdin.write(stdin_payload.encode("utf-8"))
                with contextlib.suppress(BrokenPipeError):
                    process.stdin.close()
            timeout_seconds = config["timeout_seconds"]
            deadline = (
                start + float(timeout_seconds) if timeout_seconds is not None else None
            )
            last_heartbeat = time.monotonic()
            while True:
                request = load_cancel_request(task_dir)
                if request is not None:
                    cancellation = request
                    write_descriptor(
                        {
                            "status": "cancelling",
                            "cancellation_request_path": str(
                                cancel_request_path(task_dir)
                            ),
                        }
                    )
                    if request.get("mode") == "forced":
                        termination = force_terminate_worker(
                            process, process_group=worker_group
                        )
                    else:
                        termination = terminate_worker(
                            process,
                            process_group=worker_group,
                            reason="cancelled_graceful",
                            grace_seconds=termination_grace_seconds,
                        )
                    terminal_status = "cancelled"
                    failure_reason = str(request.get("reason") or "cancelled")
                    exit_code = process.poll()
                    break
                poll_timeout = min(heartbeat_interval_seconds, CONTROL_POLL_SECONDS)
                if deadline is not None:
                    poll_timeout = min(
                        poll_timeout,
                        max(deadline - time.monotonic(), 0.1),
                    )
                try:
                    exit_code = process.wait(timeout=poll_timeout)
                    break
                except subprocess.TimeoutExpired:
                    if deadline is not None and time.monotonic() >= deadline:
                        termination = terminate_worker(
                            process,
                            process_group=worker_group,
                            reason="timeout",
                            grace_seconds=termination_grace_seconds,
                        )
                        terminal_status = "timed_out"
                        failure_reason = f"worker exceeded {timeout_seconds} seconds"
                        break
                    if time.monotonic() - last_heartbeat >= heartbeat_interval_seconds:
                        stdout_bytes = stdout_path.stat().st_size
                        stderr_bytes = stderr_path.stat().st_size
                        output_bytes = stdout_bytes + stderr_bytes
                        if output_bytes > last_output_bytes:
                            last_output_growth_at = core.utc_now()
                        output_delta = max(output_bytes - last_output_bytes, 0)
                        last_output_bytes = output_bytes
                        heartbeat_count += 1
                        write_descriptor(
                            {
                                "status": "running",
                                "worker_pid": process.pid,
                                "last_alive_at": core.utc_now(),
                                "progress": {
                                    "heartbeat_count": heartbeat_count,
                                    "stdout_bytes": stdout_bytes,
                                    "stderr_bytes": stderr_bytes,
                                    "output_bytes_delta": output_delta,
                                    "last_output_growth_at": last_output_growth_at,
                                    "updated_at": core.utc_now(),
                                },
                            }
                        )
                        lease = worker_lease.renew_lease(lease, task_dir)
                        last_heartbeat = time.monotonic()
    except OSError as error:
        terminal_status = "failed"
        failure_reason = str(error)
    duration_seconds = time.monotonic() - start
    if terminal_status == "completed" and exit_code != 0:
        terminal_status = "failed"
        failure_reason = f"worker exited with code {exit_code}"
        if worker_diagnostics.classify_rate_limit(stdout_path, stderr_path):
            terminal_status = "rate_limited"
            failure_reason = "worker output matched a rate-limit diagnostic"

    result = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_RESULT",
        "task_id": task_id,
        "worker": worker,
        "terminal_status": terminal_status,
        "exit_code": exit_code,
        "failure_reason": failure_reason,
        "duration_seconds": round(duration_seconds, 3),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "started_at": started_at,
        "finished_at": core.utc_now(),
    }
    if termination is not None:
        result["termination"] = termination
    if cancellation is not None:
        result["cancellation"] = cancellation
    usage: dict[str, Any] | None = None
    telemetry_error: str | None = None
    usage_adapter = config.get("usage_adapter")
    if isinstance(usage_adapter, str):
        try:
            usage = telemetry_adapters.collect(usage_adapter, stdout_path, stderr_path)
            usage.update(
                {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": "WORKER_USAGE",
                    "task_id": task_id,
                    "worker": worker,
                    "captured_at": core.utc_now(),
                }
            )
            usage_path = task_dir / "usage.json"
            core.atomic_json(usage_path, usage)
            result["usage_path"] = str(usage_path)
            result["usage"] = usage
        except (OSError, telemetry_adapters.TelemetryError) as error:
            telemetry_error = str(error)

    handoff: dict[str, Any] | None = None
    handoff_error: str | None = None
    handoff_path = task_dir / "worker-handoff.json"
    if handoff_path.is_file():
        try:
            if handoff_path.stat().st_size > 64 * 1024:
                raise WorkerError("worker handoff exceeds 65536 bytes")
            handoff = core.load_object(handoff_path)
            validate_worker_handoff(handoff)
            result["handoff_path"] = str(handoff_path)
            result["handoff_sha256"] = core.sha256_file(handoff_path)
        except (OSError, core.OrchestratorError, WorkerError) as error:
            handoff_error = str(error)

    output_manifest: dict[str, Any] | None = None
    output_collection_error: str | None = None
    try:
        output_manifest = collect_declared_outputs(declared_output_dir, task_dir)
        output_manifest.update({"task_id": task_id, "worker": worker})
        output_manifest_path = task_dir / "worker-outputs.json"
        core.atomic_json(output_manifest_path, output_manifest)
        result["output_manifest_path"] = str(output_manifest_path)
        result["declared_outputs"] = {
            "file_count": output_manifest["file_count"],
            "total_bytes": output_manifest["total_bytes"],
        }
    except (OSError, WorkerError) as error:
        output_collection_error = str(error)
    evidence = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_EVIDENCE",
        "task_id": task_id,
        "worker": worker,
        "command": command
        if config["prompt_via"] != "arg"
        else ([*config["command"], "<prompt>"]),
        "prompt_file": str(prompt),
        "prompt_sha256": source_prompt_hash,
        "effective_prompt_file": str(effective_prompt),
        "effective_prompt_sha256": core.sha256_file(effective_prompt),
        "worker_config": {
            "prompt_via": config["prompt_via"],
            "timeout_seconds": config["timeout_seconds"],
            "expect_long_running": config["expect_long_running"],
            "availability_probe_configured": config["availability_probe"] is not None,
            "policy": (
                policy_snapshot.get("name")
                if isinstance(policy_snapshot, dict)
                else None
            ),
            "usage_adapter": usage_adapter,
            "soft_duration_seconds": config.get("soft_duration_seconds"),
            "soft_output_bytes": config.get("soft_output_bytes"),
            "soft_token_budget": config.get("soft_token_budget"),
            "max_no_progress_seconds": config.get("max_no_progress_seconds"),
            "warnings": config["warnings"],
            **config["extras"],
        },
        "started_at": started_at,
        "finished_at": result["finished_at"],
    }
    availability_preflight = descriptor_snapshot.get("availability_preflight")
    if isinstance(availability_preflight, dict):
        evidence["availability_preflight"] = availability_preflight
    intent_admission = descriptor_snapshot.get("intent_admission")
    if isinstance(intent_admission, dict):
        evidence["intent_admission"] = intent_admission
    if wake_target is not None:
        evidence["wake_target"] = wake_target
    if policy_snapshot is not None:
        evidence["worker_policy"] = policy_snapshot
    if cancellation is not None:
        evidence["cancellation"] = cancellation
        ack = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "WORKER_CONTROL_ACK",
            "task_id": task_id,
            "control": "cancel",
            "status": "applied",
            "terminal_status": "cancelled",
            "acknowledged_at": core.utc_now(),
        }
        core.atomic_json(task_dir / "control" / "cancel.ack.json", ack)
    if usage is not None:
        evidence["usage"] = usage
    if telemetry_error is not None:
        evidence["telemetry_error"] = telemetry_error
    if handoff is not None:
        evidence["worker_handoff"] = {
            "path": str(handoff_path),
            "sha256": core.sha256_file(handoff_path),
            "bytes": handoff_path.stat().st_size,
        }
    if handoff_error is not None:
        evidence["worker_handoff_error"] = handoff_error
    if output_manifest is not None:
        evidence["declared_outputs"] = output_manifest
    if output_collection_error is not None:
        evidence["output_collection_error"] = output_collection_error
    finalized = finalize_terminal_task(
        project,
        task_id=task_id,
        task_dir=task_dir,
        result=result,
        evidence=evidence,
        state_dir=state_dir,
        wake_target=wake_target,
    )
    final_result = finalized.get("result")
    if isinstance(final_result, dict):
        terminal_status = str(final_result.get("terminal_status", terminal_status))
        result = final_result
    worker_lease.release_lease(
        lease,
        task_dir,
        released_by="supervisor",
        terminal_status=terminal_status,
    )
    if "event_path" not in finalized:
        # Another terminal writer won. It owns evidence/event publication and
        # descriptor completion; this supervisor must not overwrite its state.
        with contextlib.suppress(OSError, core.OrchestratorError):
            descriptor_snapshot = core.load_object(descriptor_path)
        with contextlib.suppress(OSError, core.OrchestratorError, WorkerError):
            queue_tick(project, state_dir=state_dir)
        return {**descriptor_snapshot, "descriptor_path": str(descriptor_path)}

    # Values already claimed on the descriptor win: these defaults only fill in
    # what a directly supervised task (no dispatcher) never recorded.
    for key, value in {
        "prompt_sha256": source_prompt_hash,
        "effective_prompt_file": str(effective_prompt),
        "effective_prompt_sha256": core.sha256_file(effective_prompt),
    }.items():
        descriptor_snapshot.setdefault(key, value)
    terminal_updates: dict[str, Any] = {
        "status": terminal_status,
        "finished_at": result["finished_at"],
        "event_path": finalized["event_path"],
        "signal_path": finalized["signal_path"],
        "progress": {
            "heartbeat_count": heartbeat_count,
            "stdout_bytes": stdout_path.stat().st_size,
            "stderr_bytes": stderr_path.stat().st_size,
            "last_output_growth_at": last_output_growth_at,
            "updated_at": core.utc_now(),
        },
    }
    if usage is not None:
        terminal_updates["usage"] = usage
    if handoff is not None:
        terminal_updates["worker_handoff"] = {
            "path": str(handoff_path),
            "sha256": core.sha256_file(handoff_path),
            "bytes": handoff_path.stat().st_size,
        }
    if output_manifest is not None:
        terminal_updates["declared_outputs"] = {
            "manifest_path": str(task_dir / "worker-outputs.json"),
            "file_count": output_manifest["file_count"],
            "total_bytes": output_manifest["total_bytes"],
        }
    if output_collection_error is not None:
        terminal_updates["output_collection_error"] = output_collection_error
    write_descriptor(terminal_updates)
    release_dispatch_claim(project, descriptor_snapshot, state_dir=state_dir)
    lineage = descriptor_snapshot.get("retry_lineage")
    if terminal_status == "completed" and isinstance(lineage, dict):
        parent_task_id = lineage.get("parent_task_id")
        if isinstance(parent_task_id, str):
            with contextlib.suppress(
                OSError,
                core.OrchestratorError,
                task_resolution.TaskResolutionError,
            ):
                task_resolution.write_resolution(
                    project,
                    task_id=parent_task_id,
                    status="superseded",
                    reason=f"retry completed as {task_id}",
                    superseded_by_task_id=task_id,
                    state_dir=state_dir,
                )
    with contextlib.suppress(OSError, core.OrchestratorError, WorkerError):
        queue_tick(project, state_dir=state_dir)
    return {**descriptor_snapshot, "descriptor_path": str(descriptor_path)}


def reap_worker_tasks(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Finalize tasks whose leased supervisor is proven gone.

    Reaping is conservative: legacy tasks without a lease and live supervisors
    are reported but never changed. Repeated calls are idempotent because the
    terminal result and event use exclusive/deterministic identities.
    """
    project = project_root.expanduser().resolve()
    current = now or datetime.now(UTC)
    tasks_root = core.state_root(project, state_dir=state_dir) / "tasks"
    outcomes: list[dict[str, Any]] = []
    for descriptor_path in sorted(tasks_root.glob("*/task.json")):
        try:
            descriptor = core.load_object(descriptor_path)
        except (OSError, core.OrchestratorError) as error:
            outcomes.append(
                {
                    "task_id": descriptor_path.parent.name,
                    "status": "invalid",
                    "reason": str(error),
                }
            )
            continue
        task_id = descriptor.get("task_id")
        status = descriptor.get("status")
        if not isinstance(task_id, str) or status not in {
            "starting",
            "running",
            "cancelling",
        }:
            continue
        task_dir = descriptor_path.parent
        try:
            lease = worker_lease.load_lease(worker_lease.lease_path(task_dir))
        except worker_lease.WorkerLeaseError as error:
            outcomes.append(
                {"task_id": task_id, "status": "invalid", "reason": str(error)}
            )
            continue
        unclaimed = False
        descriptor_age: float | None = None
        if lease is None:
            descriptor_age = worker_lease.lease_age_seconds(
                {
                    "acquired_at": descriptor.get("created_at"),
                    "renewed_at": descriptor.get("created_at"),
                },
                now=current,
            )
            if (
                descriptor.get("lease_required") is True
                and status == "starting"
                and descriptor_age is not None
                and descriptor_age > worker_lease.DEFAULT_LEASE_EXPIRY_SECONDS
            ):
                unclaimed = True
                lease = {"worker": descriptor.get("worker")}
            else:
                outcomes.append({"task_id": task_id, "status": "legacy_unleased"})
                continue
        if lease.get("status") == "released":
            continue
        age = (
            descriptor_age
            if unclaimed
            else worker_lease.lease_age_seconds(lease, now=current)
        )
        expiry = lease.get("lease_expiry_seconds")
        if not isinstance(expiry, (int, float)) or isinstance(expiry, bool):
            expiry = worker_lease.DEFAULT_LEASE_EXPIRY_SECONDS
        if age is None or age <= float(expiry):
            continue
        supervisor_state = (
            {"state": "gone", "identity_verified": False, "observed": None}
            if unclaimed
            else worker_lease.identity_state(lease.get("supervisor_identity"))
        )
        if supervisor_state["state"] == "alive":
            outcomes.append(
                {
                    "task_id": task_id,
                    "status": "stale_supervisor_alive",
                    "lease_age_seconds": age,
                }
            )
            continue
        if supervisor_state["state"] == "unknown":
            outcomes.append(
                {
                    "task_id": task_id,
                    "status": "unsafe_missing_identity",
                    "lease_age_seconds": age,
                }
            )
            continue

        termination = worker_lease.stop_worker_tree(
            worker_pid=lease.get("worker_pid"),
            worker_pgid=lease.get("worker_pgid"),
            worker_identity=lease.get("worker_identity"),
            reason="supervisor_lost",
        )
        finished_at = core.utc_now()
        worker = str(descriptor.get("worker") or lease.get("worker") or "unknown")
        result = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "WORKER_RESULT",
            "task_id": task_id,
            "worker": worker,
            "terminal_status": "failed",
            "exit_code": None,
            "failure_reason": "supervisor_lost",
            "failure_class": "supervisor_lost",
            "duration_seconds": 0.0,
            "stdout_path": str(task_dir / "worker-stdout.log"),
            "stderr_path": str(task_dir / "worker-stderr.log"),
            "started_at": descriptor.get("created_at", finished_at),
            "finished_at": finished_at,
            "termination": termination,
        }
        evidence = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "WORKER_EVIDENCE",
            "task_id": task_id,
            "worker": worker,
            "command": ["<unavailable: supervisor lost>"],
            "prompt_file": str(descriptor.get("prompt_file", "")),
            "prompt_sha256": descriptor.get("prompt_sha256"),
            "worker_config": {"recovered_by": "worker reap"},
            "started_at": descriptor.get("created_at", finished_at),
            "finished_at": finished_at,
            "recovery": {
                "reason": "supervisor_lost",
                "lease_path": str(worker_lease.lease_path(task_dir)),
                "lease_age_seconds": age,
                "supervisor_identity_state": supervisor_state,
            },
        }
        for key in ("effective_prompt_file", "effective_prompt_sha256"):
            value = descriptor.get(key)
            if isinstance(value, str):
                evidence[key] = value
        wake_target = descriptor.get("wake_target")
        if isinstance(wake_target, dict):
            evidence["wake_target"] = wake_target
        finalized = finalize_terminal_task(
            project,
            task_id=task_id,
            task_dir=task_dir,
            result=result,
            evidence=evidence,
            state_dir=state_dir,
            wake_target=wake_target if isinstance(wake_target, dict) else None,
            takeover=True,
        )
        final_result = finalized.get("result")
        if not isinstance(final_result, dict):
            outcomes.append(
                {
                    "task_id": task_id,
                    "status": "conflict",
                    "reason": finalized.get("conflict"),
                }
            )
            continue
        descriptor.update(
            {
                "status": final_result["terminal_status"],
                "finished_at": final_result["finished_at"],
                "event_path": finalized["event_path"],
                "signal_path": finalized["signal_path"],
                "reaped_at": core.utc_now(),
            }
        )
        core.atomic_json(descriptor_path, descriptor)
        release_dispatch_claim(project, descriptor, state_dir=state_dir)
        if not unclaimed:
            worker_lease.release_lease(
                lease,
                task_dir,
                released_by="reaper",
                terminal_status=str(final_result["terminal_status"]),
            )
        outcomes.append(
            {
                "task_id": task_id,
                "status": "reaped",
                "terminal_status": final_result["terminal_status"],
            }
        )
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "WORKER_REAP_REPORT",
        "project_root": str(project),
        "reaped_count": sum(item.get("status") == "reaped" for item in outcomes),
        "outcomes": outcomes,
    }
