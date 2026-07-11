"""Host binding contract for deterministic completion delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import core

BINDING_KIND = "ORCHESTRATOR_BINDING"
WAKE_TARGET_KIND = "ORCHESTRATOR_WAKE_TARGET"
SUPPORTED_HOSTS = {"codex", "claude", "vscode"}
HOSTS_REQUIRING_THREAD_ID = {"codex"}


class BindingError(RuntimeError):
    """A deterministic binding contract failure."""


def binding_path(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return core.inbox_root(project_root, state_dir=state_dir) / "binding.json"


def write_binding(
    project_root: Path,
    *,
    host: str,
    target_thread_id: str | None = None,
    codex_command: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    if host not in SUPPORTED_HOSTS:
        raise BindingError(f"unsupported host: {host}")
    if host in HOSTS_REQUIRING_THREAD_ID and not target_thread_id:
        raise BindingError(f"host {host} requires a target thread id")
    binding = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": BINDING_KIND,
        "host": host,
        "created_at": core.utc_now(),
    }
    if target_thread_id:
        binding["target_thread_id"] = target_thread_id
    if codex_command:
        binding["codex_command"] = codex_command
    path = binding_path(project_root, state_dir=state_dir)
    core.atomic_json(path, binding)
    return {**binding, "binding_path": str(path)}


def load_binding(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any] | None:
    path = binding_path(project_root, state_dir=state_dir)
    if not path.exists():
        return None
    binding = core.load_object(path)
    validate_binding(binding)
    return {**binding, "binding_path": str(path)}


def require_binding(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    binding = load_binding(project_root, state_dir=state_dir)
    if binding is None:
        raise BindingError(
            "no binding found; run `orchestrator-engine bind --host ...` "
            "from the host chat first"
        )
    return binding


def validate_binding(binding: dict[str, Any]) -> None:
    if not core.is_supported_schema_version(binding.get("schema_version")):
        raise BindingError("unsupported binding schema")
    if binding.get("kind") != BINDING_KIND:
        raise BindingError("unsupported binding kind")
    host = binding.get("host")
    if host not in SUPPORTED_HOSTS:
        raise BindingError(f"binding has unsupported host: {host}")
    thread_id = binding.get("target_thread_id")
    if host in HOSTS_REQUIRING_THREAD_ID and (
        not isinstance(thread_id, str) or not thread_id
    ):
        raise BindingError(f"binding for host {host} is missing target_thread_id")
    codex_command = binding.get("codex_command")
    if codex_command is not None and not isinstance(codex_command, str):
        raise BindingError("binding codex_command must be a string")


def wake_target_from_binding(binding: dict[str, Any]) -> dict[str, Any]:
    validate_binding(binding)
    target = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": WAKE_TARGET_KIND,
        "host": binding["host"],
        "captured_at": core.utc_now(),
    }
    if "target_thread_id" in binding:
        target["target_thread_id"] = binding["target_thread_id"]
    if "codex_command" in binding:
        target["codex_command"] = binding["codex_command"]
    return target


def validate_wake_target(target: dict[str, Any]) -> None:
    if not core.is_supported_schema_version(target.get("schema_version")):
        raise BindingError("unsupported wake target schema")
    if target.get("kind") != WAKE_TARGET_KIND:
        raise BindingError("unsupported wake target kind")
    validate_binding(
        {
            "schema_version": target["schema_version"],
            "kind": BINDING_KIND,
            "host": target.get("host"),
            **(
                {"target_thread_id": target["target_thread_id"]}
                if "target_thread_id" in target
                else {}
            ),
            **(
                {"codex_command": target["codex_command"]}
                if "codex_command" in target
                else {}
            ),
        }
    )


def clear_binding(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    path = binding_path(project_root, state_dir=state_dir)
    existed = path.is_file()
    if existed:
        path.unlink()
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": BINDING_KIND,
        "status": "cleared" if existed else "absent",
        "binding_path": str(path),
    }
