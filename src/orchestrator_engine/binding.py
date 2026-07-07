"""Host binding contract: which chat the watcher should wake."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import core

BINDING_KIND = "ORCHESTRATOR_BINDING"
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
    if binding.get("schema_version") != core.SCHEMA_VERSION:
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
