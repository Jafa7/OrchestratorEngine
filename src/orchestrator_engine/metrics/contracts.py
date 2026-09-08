"""Versioned contracts and canonical serialization for metrics records."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1
SOURCE_KIND = "ORCHESTRATOR_METRICS_SOURCE"
OBSERVATION_KIND = "ORCHESTRATOR_METRICS_OBSERVATION"
GENERATION_KIND = "ORCHESTRATOR_METRICS_GENERATION"
REPORT_KIND = "ORCHESTRATOR_METRICS_REPORT"
GUIDANCE_KIND = "ORCHESTRATOR_METRICS_GUIDANCE"
PROGRESS_KIND = "ORCHESTRATOR_METRICS_PROGRESS"

MAX_OBSERVATION_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_IMPORT_RECORDS = 1000
RECORD_TYPES = frozenset(
    {
        "source_capability",
        "execution_attempt",
        "work_item",
        "classification",
        "acceptance",
        "quota_sample",
        "delivery",
        "obligation",
        "report_snapshot",
        "scope_revision",
        "scope_item",
    }
)
CAPABILITY_STATUSES = frozenset(
    {
        "supported",
        "not_supported",
        "disabled",
        "temporarily_unavailable",
        "not_applicable",
    }
)


class MetricsContractError(ValueError):
    """A metrics record does not satisfy the public bounded contract."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def content_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetricsContractError(f"{field} must be a non-empty string")
    return value


def require_bounded_text(value: object, field: str, maximum: int) -> str:
    text = require_text(value, field)
    if len(text) > maximum:
        raise MetricsContractError(f"{field} exceeds {maximum} characters")
    return text


def require_timestamp(value: object, field: str) -> str:
    text = require_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise MetricsContractError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise MetricsContractError(f"{field} must include a timezone")
    return text


def normalize_source(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise MetricsContractError("unsupported metrics source schema_version")
    if value.get("kind") != SOURCE_KIND:
        raise MetricsContractError("invalid metrics source kind")
    source_id = require_text(value.get("source_id"), "source_id")
    try:
        uuid.UUID(source_id)
    except ValueError as error:
        raise MetricsContractError("source_id must be a UUID") from error
    capabilities = value.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) and item for item in capabilities
    ):
        raise MetricsContractError("capabilities must be a list of strings")
    inventory = value.get("capability_inventory")
    if inventory is None:
        inventory = [_default_capability(item) for item in sorted(set(capabilities))]
    if not isinstance(inventory, list) or not all(
        isinstance(item, dict) for item in inventory
    ):
        raise MetricsContractError("capability_inventory must be a list of objects")
    normalized_inventory = sorted(
        (_normalize_capability(item) for item in inventory),
        key=lambda item: item["name"],
    )
    inventory_names = [item["name"] for item in normalized_inventory]
    if len(inventory_names) != len(set(inventory_names)):
        raise MetricsContractError("capability_inventory names must be unique")
    if set(inventory_names) != set(capabilities):
        raise MetricsContractError(
            "capability_inventory names must match capabilities"
        )
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MetricsContractError("enabled must be a boolean")
    observation_semantics = require_text(
        value.get("observation_semantics", "mixed"),
        "observation_semantics",
    )
    if observation_semantics not in {
        "immutable_events",
        "mutable_snapshots",
        "mixed",
    }:
        raise MetricsContractError("unsupported observation_semantics")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": SOURCE_KIND,
        "source_id": source_id,
        "name": require_text(value.get("name"), "name"),
        "source_type": require_text(value.get("source_type"), "source_type"),
        "adapter_version": require_text(
            value.get("adapter_version", "unspecified"), "adapter_version"
        ),
        "authority": require_text(
            value.get("authority", "source_asserted"), "authority"
        ),
        "identity_mapping": require_text(
            value.get("identity_mapping", "explicit_native_identity"),
            "identity_mapping",
        ),
        "observation_semantics": observation_semantics,
        "scope": require_text(value.get("scope", "project"), "scope"),
        "enabled": enabled,
        "capabilities": sorted(set(capabilities)),
        "capability_inventory": normalized_inventory,
        "created_at": require_timestamp(value.get("created_at"), "created_at"),
    }


def _default_capability(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "supported",
        "units": [],
        "counter_semantics": "unspecified",
        "precision": "unknown",
        "clock": "source",
        "freshness": "point_in_time",
        "permissions": "local_read",
        "overhead": "bounded",
    }


def _normalize_capability(value: dict[str, Any]) -> dict[str, Any]:
    status = require_text(value.get("status"), "capability.status")
    if status not in CAPABILITY_STATUSES:
        raise MetricsContractError(f"unsupported capability status: {status}")
    units = value.get("units")
    if not isinstance(units, list) or not all(isinstance(item, str) for item in units):
        raise MetricsContractError("capability.units must be a list of strings")
    return {
        "name": require_text(value.get("name"), "capability.name"),
        "status": status,
        "units": sorted(set(units)),
        "counter_semantics": require_text(
            value.get("counter_semantics"), "capability.counter_semantics"
        ),
        "precision": require_text(value.get("precision"), "capability.precision"),
        "clock": require_text(value.get("clock"), "capability.clock"),
        "freshness": require_text(value.get("freshness"), "capability.freshness"),
        "permissions": require_text(value.get("permissions"), "capability.permissions"),
        "overhead": require_text(value.get("overhead"), "capability.overhead"),
    }


def normalize_observation(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise MetricsContractError("unsupported metrics observation schema_version")
    if value.get("kind") != OBSERVATION_KIND:
        raise MetricsContractError("invalid metrics observation kind")
    record_type = require_text(value.get("record_type"), "record_type")
    if record_type not in RECORD_TYPES:
        raise MetricsContractError(f"unsupported record_type: {record_type}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise MetricsContractError("data must be an object")
    _validate_record_data(record_type, data)
    scope = value.get("scope", {})
    if not isinstance(scope, dict):
        raise MetricsContractError("scope must be an object")
    observed_at = require_timestamp(value.get("observed_at"), "observed_at")
    source_id = require_text(value.get("source_id"), "source_id")
    try:
        uuid.UUID(source_id)
    except ValueError as error:
        raise MetricsContractError("source_id must be a UUID") from error
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": OBSERVATION_KIND,
        "observation_id": require_bounded_text(
            value.get("observation_id"), "observation_id", 256
        ),
        "source_id": source_id,
        "observed_at": observed_at,
        "effective_at": require_timestamp(
            value.get("effective_at", observed_at), "effective_at"
        ),
        "known_at": require_timestamp(value.get("known_at", observed_at), "known_at"),
        "record_type": record_type,
        "scope": scope,
        "data": data,
    }
    if len(canonical_bytes(normalized)) > MAX_OBSERVATION_BYTES:
        raise MetricsContractError(
            f"normalized observation exceeds {MAX_OBSERVATION_BYTES} bytes"
        )
    return normalized


def _validate_record_data(record_type: str, data: dict[str, Any]) -> None:
    observation_mode = data.get("observation_mode")
    if observation_mode is not None and observation_mode not in {
        "full_snapshot",
        "partial_update",
        "complement",
    }:
        raise MetricsContractError("unsupported data.observation_mode")
    for field in ("execution_kind", "operation_kind"):
        if data.get(field) is not None:
            require_bounded_text(data[field], f"data.{field}", 128)
    attempt_sequence = data.get("attempt_sequence")
    if attempt_sequence is not None and (
        isinstance(attempt_sequence, bool)
        or not isinstance(attempt_sequence, int)
        or attempt_sequence < 1
    ):
        raise MetricsContractError("data.attempt_sequence must be a positive integer")
    canonical_identity = data.get("canonical_identity")
    if canonical_identity is not None:
        if not isinstance(canonical_identity, dict):
            raise MetricsContractError("data.canonical_identity must be an object")
        require_bounded_text(
            canonical_identity.get("namespace"),
            "data.canonical_identity.namespace",
            256,
        )
        require_bounded_text(
            canonical_identity.get("id"), "data.canonical_identity.id", 256
        )
    if record_type == "scope_revision":
        require_bounded_text(
            data.get("scope_revision_id"), "data.scope_revision_id", 256
        )
        revision_index = data.get("revision_index")
        if (
            isinstance(revision_index, bool)
            or not isinstance(revision_index, int)
            or revision_index < 0
        ):
            raise MetricsContractError(
                "data.revision_index must be a non-negative integer"
            )
        for field in ("baseline", "current"):
            if field in data and not isinstance(data[field], bool):
                raise MetricsContractError(f"data.{field} must be a boolean")
        if "change_reason" in data:
            require_bounded_text(data["change_reason"], "data.change_reason", 512)
    elif record_type == "scope_item":
        require_bounded_text(
            data.get("scope_revision_id"), "data.scope_revision_id", 256
        )
        require_bounded_text(data.get("work_item_id"), "data.work_item_id", 256)
        require_bounded_text(
            data.get("criteria_revision"), "data.criteria_revision", 256
        )
        status = require_text(data.get("status"), "data.status")
        if status not in {
            "planned",
            "in_progress",
            "blocked",
            "review_ready",
            "accepted",
            "reopened",
            "removed",
        }:
            raise MetricsContractError(f"unsupported scope item status: {status}")
        if "weight" in data:
            weight = data["weight"]
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or weight <= 0
            ):
                raise MetricsContractError("data.weight must be a positive number")
        for field in ("module_id", "work_class", "change_reason"):
            if field in data:
                require_bounded_text(data[field], f"data.{field}", 512)
        for field in ("parent_work_item_ids", "dependency_ids"):
            if field not in data:
                continue
            values = data[field]
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item for item in values
            ):
                raise MetricsContractError(f"data.{field} must be a list of strings")
        for field in ("started_at", "accepted_at"):
            if field in data:
                require_timestamp(data[field], f"data.{field}")
    elif record_type == "acceptance":
        applicability = (
            "scope_revision_id" in data or "criteria_revision" in data
        )
        if applicability:
            require_bounded_text(
                data.get("scope_revision_id"), "data.scope_revision_id", 256
            )
            require_bounded_text(
                data.get("criteria_revision"), "data.criteria_revision", 256
            )
        if "carried_from_scope_revision_id" in data:
            if not applicability:
                raise MetricsContractError(
                    "acceptance carryover requires scope and criteria revisions"
                )
            require_bounded_text(
                data["carried_from_scope_revision_id"],
                "data.carried_from_scope_revision_id",
                256,
            )
        if "completion_cycle_id" in data:
            require_bounded_text(
                data["completion_cycle_id"], "data.completion_cycle_id", 256
            )


def make_observation(
    *,
    source_id: str,
    record_type: str,
    data: dict[str, Any],
    observed_at: str | None = None,
    observation_id: str | None = None,
    effective_at: str | None = None,
    known_at: str | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = observed_at or utc_now()
    base = {
        "schema_version": SCHEMA_VERSION,
        "kind": OBSERVATION_KIND,
        "observation_id": observation_id or str(uuid.uuid4()),
        "source_id": source_id,
        "observed_at": timestamp,
        "effective_at": effective_at or timestamp,
        "known_at": known_at or timestamp,
        "record_type": record_type,
        "scope": scope or {},
        "data": data,
    }
    return normalize_observation(base)
