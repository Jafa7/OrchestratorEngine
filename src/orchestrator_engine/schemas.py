"""Packaged JSON Schema catalog for the public v0.1 durable artifacts."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

SCHEMA_VERSION = 1
SCHEMA_DIR = "schemas"
SCHEMA_NAMES = (
    "worker-task",
    "worker-result",
    "worker-evidence",
    "worker-policy-snapshot",
    "worker-lease",
    "worker-handoff",
    "worker-usage",
    "worker-output-manifest",
    "worker-queue-entry",
    "worker-cancel-request",
    "worker-control-ack",
    "worker-task-intent",
    "worker-dispatch-claim",
    "terminal-event",
    "inbox-signal",
    "binding",
    "wake-target",
    "verification-result",
    "task-resolution",
)
KIND = "ORCHESTRATOR_SCHEMA_CATALOG"


def schema_path(name: str):
    if name not in SCHEMA_NAMES:
        raise ValueError(f"unknown schema name: {name}")
    return files("orchestrator_engine").joinpath(SCHEMA_DIR, f"{name}.json")


def load(name: str) -> dict[str, Any]:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def catalog() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "schema_count": len(SCHEMA_NAMES),
        "schemas": list(SCHEMA_NAMES),
    }
