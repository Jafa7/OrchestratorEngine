"""Versioned, provider-neutral host delivery capability contract."""

from __future__ import annotations

from typing import Any

from . import core

KIND = "ORCHESTRATOR_HOST_CAPABILITIES"
DELIVERY_MODES = frozenset(
    {
        "session_stream",
        "session_queue",
        "ui_injection",
        "headless_app_server_turn",
    }
)
LIVE_REFRESH_SUPPORT = frozenset({"supported", "best_effort", "unsupported"})

_CAPABILITIES: dict[str, dict[str, Any]] = {
    "claude": {
        "delivery_mode": "session_stream",
        "live_refresh_support": "supported",
    },
    "vscode": {
        "delivery_mode": "ui_injection",
        "live_refresh_support": "best_effort",
    },
    "codex": {
        "delivery_mode": "session_queue",
        "live_refresh_support": "supported",
        "requirement": "codex queue",
        "fallback_delivery_mode": "headless_app_server_turn",
        "fallback_live_refresh_support": "unsupported",
    },
}


def for_host(host: str) -> dict[str, Any]:
    try:
        return {"host": host, **_CAPABILITIES[host]}
    except KeyError as error:
        raise ValueError(f"unsupported host: {host}") from error


def receipt_fields(
    host: str,
    *,
    delivery_mode: str | None = None,
) -> dict[str, Any]:
    result = for_host(host)
    result.pop("host")
    if delivery_mode is None or delivery_mode == result["delivery_mode"]:
        return result
    fallback_mode = result.get("fallback_delivery_mode")
    if delivery_mode != fallback_mode:
        raise ValueError(f"unsupported {host} delivery mode: {delivery_mode}")
    return {
        "delivery_mode": fallback_mode,
        "live_refresh_support": result["fallback_live_refresh_support"],
    }


def all_hosts() -> dict[str, Any]:
    """Return the bounded public capability report in stable host order."""

    hosts = [for_host(host) for host in sorted(_CAPABILITIES)]
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": KIND,
        "host_count": len(hosts),
        "hosts": hosts,
    }
