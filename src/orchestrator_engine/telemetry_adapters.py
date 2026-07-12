"""Explicit, provider-boundary usage telemetry adapters.

Adapters only summarize worker output as data. Their results never influence
task success, retries, model selection or permissions.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_SCAN_BYTES = 2 * 1024 * 1024
TOKEN_KEYS = {
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
}


class TelemetryError(RuntimeError):
    """An explicitly configured telemetry adapter cannot read its input."""


def nested_token_counts(value: object, counts: dict[str, int]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                key in TOKEN_KEYS
                and isinstance(item, int)
                and not isinstance(item, bool)
            ):
                counts[key] = counts.get(key, 0) + item
            else:
                nested_token_counts(item, counts)
    elif isinstance(value, list):
        for item in value:
            nested_token_counts(item, counts)


def json_lines_usage(stdout_path: Path, stderr_path: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    parsed_records = 0
    for path in (stdout_path, stderr_path):
        try:
            raw = path.read_bytes()[-MAX_SCAN_BYTES:]
        except OSError:
            continue
        for line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed_records += 1
            nested_token_counts(value, counts)
    total = sum(
        value
        for key, value in counts.items()
        if key in {"input_tokens", "output_tokens"}
    )
    return {
        "adapter": "json-lines-usage",
        "parsed_records": parsed_records,
        "token_counts": counts,
        "total_tokens": total,
    }


USAGE_ADAPTERS: dict[str, Callable[[Path, Path], dict[str, Any]]] = {
    "json-lines-usage": json_lines_usage,
}


def collect(name: str, stdout_path: Path, stderr_path: Path) -> dict[str, Any]:
    adapter = USAGE_ADAPTERS.get(name)
    if adapter is None:
        raise TelemetryError(f"unknown usage adapter: {name}")
    return adapter(stdout_path, stderr_path)
