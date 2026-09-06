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
CODEX_TOKEN_KEYS = TOKEN_KEYS | {
    "cache_write_input_tokens",
    "reasoning_output_tokens",
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
                and item >= 0
            ):
                counts[key] = counts.get(key, 0) + item
            else:
                nested_token_counts(item, counts)
    elif isinstance(value, list):
        for item in value:
            nested_token_counts(item, counts)


def bounded_tail(path: Path) -> bytes:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(size - MAX_SCAN_BYTES, 0))
        return stream.read(MAX_SCAN_BYTES)


def source_metadata(path: Path, scanned_bytes: int) -> dict[str, int | bool]:
    size = path.stat().st_size
    return {
        "source_bytes": size,
        "scanned_bytes": scanned_bytes,
        "truncated": size > scanned_bytes,
    }


def json_lines_usage(stdout_path: Path, stderr_path: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    parsed_records = 0
    usage_records = 0
    source_bytes = 0
    scanned_bytes = 0
    truncated = False
    for path in (stdout_path, stderr_path):
        try:
            raw = bounded_tail(path)
            metadata = source_metadata(path, len(raw))
        except OSError:
            continue
        source_bytes += int(metadata["source_bytes"])
        scanned_bytes += int(metadata["scanned_bytes"])
        truncated = truncated or bool(metadata["truncated"])
        for line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except (ValueError, RecursionError):
                continue
            parsed_records += 1
            before = dict(counts)
            nested_token_counts(value, counts)
            if counts != before:
                usage_records += 1
    total = sum(
        value
        for key, value in counts.items()
        if key in {"input_tokens", "output_tokens"}
    )
    return {
        "adapter": "json-lines-usage",
        "measurement_status": (
            "partial"
            if truncated
            else "unverified"
            if usage_records
            else "unavailable"
        ),
        "parsed_records": parsed_records,
        "usage_records": usage_records,
        "token_counts": counts,
        "total_tokens": total,
        "source_bytes": source_bytes,
        "scanned_bytes": scanned_bytes,
        "truncated": truncated,
    }


def codex_token_counts(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    counts: dict[str, int] = {}
    for key, item in value.items():
        if key not in CODEX_TOKEN_KEYS:
            continue
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            return None
        counts[key] = item
    if not {"input_tokens", "output_tokens"}.issubset(counts):
        return None
    return counts


def codex_json_lines_usage(
    stdout_path: Path, stderr_path: Path
) -> dict[str, Any]:
    """Read the final aggregate usage record emitted by `codex exec --json`."""

    source_bytes = 0
    scanned_bytes = 0
    truncated = False
    raw_stdout = b""
    for path in (stdout_path, stderr_path):
        try:
            raw = bounded_tail(path)
            metadata = source_metadata(path, len(raw))
        except OSError:
            continue
        source_bytes += int(metadata["source_bytes"])
        scanned_bytes += int(metadata["scanned_bytes"])
        truncated = truncated or bool(metadata["truncated"])
        if path == stdout_path:
            raw_stdout = raw

    parsed_records = 0
    usage_records = 0
    invalid_usage_records = 0
    final_counts: dict[str, int] | None = None
    for line in raw_stdout.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            continue
        parsed_records += 1
        if not isinstance(value, dict) or value.get("type") != "turn.completed":
            continue
        usage_records += 1
        counts = codex_token_counts(value.get("usage"))
        if counts is None:
            invalid_usage_records += 1
            final_counts = None
            continue
        final_counts = counts

    total = (
        final_counts["input_tokens"] + final_counts["output_tokens"]
        if final_counts is not None
        else 0
    )
    return {
        "adapter": "codex-jsonl-usage",
        "measurement_status": (
            "complete"
            if final_counts is not None
            else "partial"
            if truncated or invalid_usage_records
            else "unavailable"
        ),
        "parsed_records": parsed_records,
        "usage_records": usage_records,
        "invalid_usage_records": invalid_usage_records,
        "token_counts": final_counts or {},
        "total_tokens": total,
        "source_bytes": source_bytes,
        "scanned_bytes": scanned_bytes,
        "truncated": truncated,
    }


USAGE_ADAPTERS: dict[str, Callable[[Path, Path], dict[str, Any]]] = {
    "json-lines-usage": json_lines_usage,
    "codex-jsonl-usage": codex_json_lines_usage,
}


def collect(name: str, stdout_path: Path, stderr_path: Path) -> dict[str, Any]:
    adapter = USAGE_ADAPTERS.get(name)
    if adapter is None:
        raise TelemetryError(f"unknown usage adapter: {name}")
    return adapter(stdout_path, stderr_path)
