"""VS Code chat adapter: wake the last active window's chat view."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import core, host_capabilities, wakeup


class VscodeChatError(RuntimeError):
    """A deterministic VS Code chat adapter failure."""


DEFAULT_CODE_COMMAND = "code"
CHAT_TIMEOUT_SECONDS = 30


def chat_wakeup_receipt_path(
    project_root: Path,
    event_id: str,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> Path:
    return (
        core.inbox_root(project_root, state_dir=state_dir)
        / "thread-wakeups"
        / f"{event_id}.json"
    )


def wake_chat(
    project_root: Path,
    signal: dict[str, Any],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    code: str = DEFAULT_CODE_COMMAND,
    runner=subprocess.run,
) -> dict[str, Any]:
    event_id = signal.get("event_id")
    event_path_value = signal.get("event_path")
    if not isinstance(event_id, str) or not event_id:
        raise VscodeChatError("signal has invalid event_id")
    if not isinstance(event_path_value, str) or not event_path_value:
        raise VscodeChatError("signal has invalid event_path")

    project = project_root.expanduser().resolve()
    receipt_path = chat_wakeup_receipt_path(
        project,
        event_id,
        state_dir=state_dir,
    )
    if receipt_path.exists():
        existing = core.load_object(receipt_path)
        if existing.get("status") == "woken":
            return {
                "schema_version": core.SCHEMA_VERSION,
                "event_id": event_id,
                "status": "skipped",
                "reason": "already_woken",
                "receipt": str(receipt_path),
            }

    event = core.verify_terminal_event(
        Path(event_path_value).expanduser().resolve()
    )
    message = wakeup.build_wakeup_message(project, signal, event)
    try:
        completed = runner(
            [code, "chat", "--reuse-window", message],
            capture_output=True,
            timeout=CHAT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        receipt = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "VSCODE_CHAT_WAKEUP",
            "event_id": event_id,
            "task_id": event["task_id"],
            "status": "deferred",
            **host_capabilities.receipt_fields("vscode"),
            "reason": str(error),
            "created_at": core.utc_now(),
        }
        core.atomic_json(receipt_path, receipt)
        return {**receipt, "receipt": str(receipt_path)}
    if completed.returncode != 0:
        stderr = completed.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        receipt = {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "VSCODE_CHAT_WAKEUP",
            "event_id": event_id,
            "task_id": event["task_id"],
            "status": "deferred",
            **host_capabilities.receipt_fields("vscode"),
            "reason": (
                f"code chat exited with {completed.returncode}: "
                f"{(stderr or '').strip()[:500]}"
            ),
            "created_at": core.utc_now(),
        }
        core.atomic_json(receipt_path, receipt)
        return {**receipt, "receipt": str(receipt_path)}
    receipt = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": "VSCODE_CHAT_WAKEUP",
        "event_id": event_id,
        "task_id": event["task_id"],
        "status": "woken",
        **host_capabilities.receipt_fields("vscode"),
        "created_at": core.utc_now(),
    }
    core.atomic_json(receipt_path, receipt)
    return {**receipt, "receipt": str(receipt_path)}
