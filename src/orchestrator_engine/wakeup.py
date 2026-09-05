"""Host-neutral wakeup message contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

WAKEUP_HEADER = "LOCAL_AI_ORCHESTRATOR_WAKEUP v1"


def build_wakeup_message(
    project_root: Path,
    signal: dict[str, Any],
    event: dict[str, Any],
) -> str:
    subject = (
        [f"task_id: {event['task_id']}"]
        if isinstance(event.get("task_id"), str)
        else [
            f"source: {event['source_kind']}",
            f"operation_id: {event['operation_id']}",
        ]
    )
    return "\n".join(
        [
            WAKEUP_HEADER,
            f"project: {project_root}",
            f"event_id: {event['event_id']}",
            *subject,
            f"terminal_status: {event['terminal_status']}",
            f"event: {signal['event_path']}",
            f"evidence: {event['evidence_path']}",
            f"result: {event['result_path']}",
            "requires: ORCHESTRATOR_FOLLOWUP",
            "",
            "Read the event/evidence. Verify state and decide the next safe action.",
            "If review is required, inspect the real diff and checks before accepting.",
            "Do not commit or push unless the user explicitly requested it.",
        ]
    )
