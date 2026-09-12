"""Deterministic calculations over normalized metrics observations."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Hashable
from datetime import datetime
from itertools import pairwise
from typing import Any

from .catalog import METRIC_DEFINITIONS


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _time_order(value: object) -> float:
    timestamp = _timestamp(value)
    return timestamp if timestamp is not None else float("-inf")


def _source_key(record: dict[str, Any], value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value:
        return None
    return record["source_id"], value


def _identity(
    record: dict[str, Any], *, canonical_source_ids: set[str] | None = None
) -> tuple[Hashable, ...]:
    data = record["data"]
    canonical = data.get("canonical_identity")
    if (
        canonical_source_ids is not None
        and record["source_id"] in canonical_source_ids
        and isinstance(canonical, dict)
    ):
        namespace = canonical.get("namespace")
        value = canonical.get("id")
        if (
            isinstance(namespace, str)
            and namespace
            and isinstance(value, str)
            and value
        ):
            return record["record_type"], "canonical", namespace, value
    source_namespace = record["source_id"]
    if record["record_type"] == "scope_item":
        revision = data.get("scope_revision_id")
        work_item = data.get("work_item_id")
        if isinstance(revision, str) and isinstance(work_item, str):
            return source_namespace, "scope_item", revision, work_item
    if record["record_type"] == "scope_revision":
        revision = data.get("scope_revision_id")
        if isinstance(revision, str) and revision:
            return source_namespace, "scope_revision", revision
    if record["record_type"] == "acceptance":
        revision = data.get("scope_revision_id")
        criteria = data.get("criteria_revision")
        work_item = data.get("work_item_id")
        if all(
            isinstance(value, str) and value
            for value in (revision, criteria, work_item)
        ):
            return source_namespace, "acceptance", revision, criteria, work_item
    for field in (
        "execution_id",
        "event_id",
        "operation_id",
        "attempt_id",
        "work_item_id",
        "delivery_id",
        "obligation_id",
        "classification_id",
        "capability_id",
        "sample_id",
        "report_snapshot_id",
    ):
        value = data.get(field)
        if isinstance(value, str) and value:
            if field == "execution_id":
                execution_kind = data.get("execution_kind")
                if isinstance(execution_kind, str) and execution_kind:
                    return (
                        source_namespace,
                        record["record_type"],
                        field,
                        execution_kind,
                        value,
                    )
            return source_namespace, record["record_type"], field, value
    return source_namespace, "observation", record["observation_id"]


def latest_logical_records(
    observations: list[dict[str, Any]],
    *,
    canonical_source_ids: set[str] | None = None,
    source_observation_semantics: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Collapse mirrored/state-update observations by stable logical identity."""

    selected: dict[tuple[Hashable, ...], dict[str, Any]] = {}
    for record in sorted(
        observations,
        key=lambda item: (
            _time_order(item["known_at"]),
            _time_order(item["observed_at"]),
            item["observation_id"],
        ),
    ):
        key = _identity(record, canonical_source_ids=canonical_source_ids)
        previous = selected.get(key)
        if previous is None:
            selected[key] = record
        elif _merge_semantics(record, source_observation_semantics) in {
            "partial_update",
            "complement",
        }:
            selected[key] = {
                **record,
                "data": {**previous["data"], **record["data"]},
            }
        else:
            selected[key] = record
    return list(selected.values())


def _merge_semantics(
    record: dict[str, Any], source_observation_semantics: dict[str, str] | None
) -> str:
    source_semantics = (source_observation_semantics or {}).get(
        record["source_id"], "mixed"
    )
    if source_semantics != "mixed":
        return "full_snapshot"
    mode = record["data"].get("observation_mode", "full_snapshot")
    return mode if mode in {"partial_update", "complement"} else "full_snapshot"


def _result(
    definition: dict[str, Any],
    *,
    value: object,
    availability: str,
    known: int,
    total: int,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    availability_status = (
        "available" if availability in {"known", "partial"} else "unavailable"
    )
    if availability == "unknown":
        value_state = "unknown"
    elif availability == "partial":
        value_state = "partial"
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        value_state = "zero"
    else:
        value_state = "known"
    return {
        "metric_id": definition["metric_id"],
        "name": definition["name"],
        "unit": definition["unit"],
        "value": value,
        "availability": availability_status,
        "evidence_class": "observed" if known or total else "unknown",
        "value_state": value_state,
        "coverage": {
            "status": availability,
            "expected": total,
            "observed": known,
            "unknown": max(0, total - known),
            "total": total,
            "ratio": known / total if total else None,
        },
        "details": details or {},
    }


def _availability(known: int, total: int) -> str:
    if total == 0 or known == 0:
        return "unknown"
    if known < total:
        return "partial"
    return "known"


def _records(records: list[dict[str, Any]], record_type: str) -> list[dict[str, Any]]:
    return [item for item in records if item["record_type"] == record_type]


def _percentiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def nearest_rank(fraction: float) -> float:
        return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]

    return {
        "p50": nearest_rank(0.5),
        "p80": nearest_rank(0.8),
        "p95": nearest_rank(0.95),
    }


def _accepted(
    definition: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    evaluation_time: str | None = None,
) -> dict[str, Any]:
    values = _records(records, "acceptance")
    known = [item for item in values if isinstance(item["data"].get("accepted"), bool)]
    work_items = _records(records, "work_item")
    state_distribution: dict[str, int] = defaultdict(int)
    open_ages: list[float] = []
    cutoff = (
        _timestamp(evaluation_time)
        if evaluation_time is not None
        else max((_timestamp(item["known_at"]) or 0 for item in records), default=0)
    )
    cutoff = cutoff or 0
    terminal_states = {"accepted", "completed", "cancelled", "removed"}
    for item in work_items:
        status = item["data"].get("status")
        if isinstance(status, str) and status:
            state_distribution[status] += 1
        if status in terminal_states:
            continue
        started = _timestamp(
            item["data"].get("started_at") or item["data"].get("created_at")
        )
        if started is not None and cutoff >= started:
            open_ages.append(cutoff - started)
    return _result(
        definition,
        value=(sum(1 for item in known if item["data"]["accepted"]) if known else None),
        availability=_availability(len(known), len(values)),
        known=len(known),
        total=len(values),
        details={
            "work_item_state_distribution": dict(sorted(state_distribution.items())),
            "open_item_count": len(open_ages),
            "open_age_seconds": _percentiles(open_ages),
        },
    )


def _active_time(
    definition: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    values = _records(records, "execution_attempt")
    intervals: list[tuple[float, float, str | None, str | None]] = []
    for item in values:
        start = _timestamp(item["data"].get("started_at"))
        end = _timestamp(item["data"].get("finished_at"))
        if start is not None and end is not None and end >= start:
            execution_id = _source_key(item, item["data"].get("execution_id"))
            parent_source = item["data"].get("parent_execution_source_id")
            parent_value = item["data"].get("parent_execution_id")
            parent_id = (
                (parent_source, parent_value)
                if isinstance(parent_source, str)
                and parent_source
                and isinstance(parent_value, str)
                and parent_value
                else _source_key(item, parent_value)
            )
            intervals.append(
                (
                    start,
                    end,
                    execution_id,
                    parent_id,
                )
            )
    intervals.sort(key=lambda value: (value[0], value[1]))
    merged: list[list[float]] = []
    for start, end, _, _ in intervals:
        if not merged or start >= merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    value = sum(end - start for start, end in merged)
    present_ids = {execution_id for _, _, execution_id, _ in intervals if execution_id}
    root_execution = sum(
        end - start
        for start, end, _, parent_id in intervals
        if parent_id not in present_ids
    )
    nested_breakdown = sum(
        end - start
        for start, end, _, parent_id in intervals
        if parent_id in present_ids
    )
    return _result(
        definition,
        value=value if intervals else None,
        availability=_availability(len(intervals), len(values)),
        known=len(intervals),
        total=len(values),
        details={
            "interval_count": len(intervals),
            "union_count": len(merged),
            "elapsed_union_seconds": value,
            "root_execution_sum_seconds": root_execution,
            "nested_breakdown_seconds": nested_breakdown,
            "attempt_duration_seconds": _percentiles(
                [end - start for start, end, _, _ in intervals]
            ),
        },
    )


def _first_gate(
    definition: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    expected = [
        item
        for item in _records(records, "execution_attempt")
        if item["data"].get("first_gate_expected") is True
    ]
    known_statuses = {"passed", "failed", "cancelled", "pending", "not_started"}
    known = [
        item
        for item in expected
        if item["data"].get("first_gate_status") in known_statuses
    ]
    passed = sum(1 for item in known if item["data"]["first_gate_status"] == "passed")
    return _result(
        definition,
        value=passed / len(expected) if expected else None,
        availability=_availability(len(known), len(expected)),
        known=len(known),
        total=len(expected),
        details={
            "passed": passed,
            "expected_not_started": sum(
                1
                for item in known
                if item["data"]["first_gate_status"] == "not_started"
            ),
        },
    )


def _ratio(
    definition: dict[str, Any],
    records: list[dict[str, Any]],
    record_type: str,
    field: str,
) -> dict[str, Any]:
    values = _records(records, record_type)
    known = [item for item in values if isinstance(item["data"].get(field), bool)]
    positives = sum(1 for item in known if item["data"][field])
    return _result(
        definition,
        value=positives / len(known) if known else None,
        availability=_availability(len(known), len(values)),
        known=len(known),
        total=len(values),
        details={"positive": positives},
    )


def _sum_field(
    definition: dict[str, Any],
    records: list[dict[str, Any]],
    record_types: set[str],
    field: str,
) -> dict[str, Any]:
    values = [item for item in records if item["record_type"] in record_types]
    numbers = [_number(item["data"].get(field)) for item in values]
    known = [item for item in numbers if item is not None]
    return _result(
        definition,
        value=sum(known) if known else None,
        availability=_availability(len(known), len(values)),
        known=len(known),
        total=len(values),
    )


def _tokens(
    definition: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    usage_source_ids: set[str] | None = None,
) -> dict[str, Any]:
    attempts = _records(records, "execution_attempt")
    known: dict[Hashable, float] = {}
    partial: dict[Hashable, float] = {}
    unqualified = 0
    for item in attempts:
        value = _number(item["data"].get("total_tokens"))
        measurement = item["data"].get("usage_measurement_status")
        if value is not None and (
            measurement not in {"complete", "partial"}
            or (
                usage_source_ids is not None
                and item["source_id"] not in usage_source_ids
            )
        ):
            unqualified += 1
            continue
        if measurement == "partial" and value is not None:
            key = _source_key(item, item["data"].get("usage_event_id")) or _identity(
                item
            )
            partial[key] = value
            continue
        if value is None or measurement != "complete":
            continue
        key = _source_key(item, item["data"].get("usage_event_id")) or _identity(
            item
        )
        known[key] = value
    return _result(
        definition,
        value=sum(known.values()) if known else None,
        availability=(
            "partial" if partial else _availability(len(known), len(attempts))
        ),
        known=len(known) + len(partial),
        total=len(attempts),
        details={
            "deduplicated_usage_events": len(known),
            "complete_reported_tokens": sum(known.values()) if known else None,
            "partial_usage_records": len(partial),
            "partial_reported_tokens_lower_bound": sum(partial.values()) or None,
            "unqualified_usage_records": unqualified,
            **_cost_reconciliation(attempts),
        },
    )


def _cost_reconciliation(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    observed = attributed = unfinished = shared = 0.0
    known = False
    shared_ids: set[tuple[str, str]] = set()
    for item in attempts:
        data = item["data"]
        value = _number(data.get("cost_units"))
        if value is None:
            continue
        shared_id = _source_key(item, data.get("shared_cost_id"))
        if shared_id is not None:
            if shared_id in shared_ids:
                continue
            shared_ids.add(shared_id)
        known = True
        observed += value
        bucket = data.get("cost_bucket", "unallocated")
        if bucket == "accepted":
            attributed += value
        elif bucket == "unfinished":
            unfinished += value
        else:
            shared += value
    allocated = attributed + unfinished + shared
    return {
        "observed_cost_units": observed if known else None,
        "accepted_cost_units": attributed if known else None,
        "unfinished_cost_units": unfinished if known else None,
        "shared_or_unallocated_cost_units": shared if known else None,
        "cost_conservation": (
            "balanced" if known and abs(allocated - observed) < 1e-9 else "unknown"
        ),
    }


def _quota(definition: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    samples = _records(records, "quota_sample")
    latest: dict[str, dict[str, Any]] = {}
    by_bucket: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    valid_count = 0
    for item in sorted(
        samples,
        key=lambda value: (
            _time_order(value["observed_at"]),
            value["observation_id"],
        ),
    ):
        account = item["data"].get("account_alias")
        remaining = _number(item["data"].get("remaining_percentage_points"))
        if isinstance(account, str) and remaining is not None:
            valid_count += 1
            bucket = str(item["data"].get("bucket_id", "default"))
            window = str(item["data"].get("window_id", "unspecified"))
            by_bucket[(account, bucket, window)].append(item)
            latest[account] = {
                "remaining_percentage_points": remaining,
                "observed_at": item["observed_at"],
                "reset_at": item["data"].get("reset_at"),
                "rounding": item["data"].get("rounding", "unknown"),
            }
    latest_by_bucket: dict[str, dict[str, Any]] = {}
    changes: list[dict[str, Any]] = []
    for (account, bucket, window), bucket_samples in sorted(by_bucket.items()):
        ordered = sorted(
            bucket_samples,
            key=lambda value: (
                _time_order(value["observed_at"]),
                value["observation_id"],
            ),
        )
        last = ordered[-1]
        key = f"{account}:{bucket}:{window}"
        latest_by_bucket[key] = {
            "account_alias": account,
            "bucket_id": bucket,
            "window_id": window,
            "remaining_percentage_points": _number(
                last["data"].get("remaining_percentage_points")
            ),
            "observed_at": last["observed_at"],
            "reset_at": last["data"].get("reset_at"),
            "rounding": last["data"].get("rounding", "unknown"),
        }
        for before, after in pairwise(ordered):
            reset_between = (
                after["data"].get("reset_occurred") is True
                or before["data"].get("reset_at")
                != after["data"].get("reset_at")
            )
            before_value = _number(
                before["data"].get("remaining_percentage_points")
            )
            after_value = _number(after["data"].get("remaining_percentage_points"))
            changes.append(
                {
                    "account_alias": account,
                    "bucket_id": bucket,
                    "window_id": window,
                    "from_observed_at": before["observed_at"],
                    "to_observed_at": after["observed_at"],
                    "remaining_delta_percentage_points": (
                        after_value - before_value
                        if not reset_between
                        and before_value is not None
                        and after_value is not None
                        else None
                    ),
                    "reset_discontinuity": reset_between,
                    "attribution": "shared_account_observation",
                }
            )
    return _result(
        definition,
        value=latest or None,
        availability=_availability(valid_count, len(samples)),
        known=valid_count,
        total=len(samples),
        details={
            "latest_by_bucket": latest_by_bucket,
            "observed_interval_changes": changes[:100],
            "changes_truncated": len(changes) > 100,
        },
    )


def _usage_coverage(
    definition: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    attempts = _records(records, "execution_attempt")
    known = [
        item
        for item in attempts
        if _number(item["data"].get("total_tokens")) is not None
        and item["data"].get("usage_measurement_status") in {None, "complete"}
    ]
    return _result(
        definition,
        value=len(known) / len(attempts) if attempts else None,
        availability="known" if attempts else "unknown",
        known=len(attempts),
        total=len(attempts),
        details={"known_usage_attempts": len(known)},
    )


def _delivery(
    definition: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    values = _records(records, "delivery")
    terminal = {
        "delivered",
        "failed",
        "manual_required",
        "acknowledged",
        "revoked",
    }
    known = [item for item in values if item["data"].get("status") in terminal]
    delivered = sum(
        1
        for item in known
        if item["data"]["status"] in {"delivered", "acknowledged"}
    )
    return _result(
        definition,
        value=delivered / len(known) if known else None,
        availability=_availability(len(known), len(values)),
        known=len(known),
        total=len(values),
        details={"delivered": delivered},
    )


def _repetition(
    definition: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    attempts = _records(records, "execution_attempt")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    explicit = 0
    known = 0
    for item in attempts:
        data = item["data"]
        if isinstance(data.get("equivalent_repetition"), bool):
            known += 1
            explicit += int(data["equivalent_repetition"])
        key = data.get("equivalence_key")
        if isinstance(key, str) and key:
            groups[key].append(item)
    inferred = sum(
        max(0, len(items) - 1)
        for items in groups.values()
        if all(item["data"].get("evidence_delta") is False for item in items[1:])
    )
    value = max(explicit, inferred)
    classified = max(known, sum(len(items) for items in groups.values()))
    return _result(
        definition,
        value=value if classified else None,
        availability=_availability(min(classified, len(attempts)), len(attempts)),
        known=min(classified, len(attempts)),
        total=len(attempts),
        details={"explicit": explicit, "inferred": inferred},
    )


CALCULATORS: dict[
    str, Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
] = {
    "MET-001": _accepted,
    "MET-002": _active_time,
    "MET-003": _first_gate,
    "MET-004": lambda definition, records: _ratio(
        definition, records, "work_item", "reworked"
    ),
    "MET-005": lambda definition, records: _sum_field(
        definition, records, {"work_item"}, "human_attention_seconds"
    ),
    "MET-006": lambda definition, records: _sum_field(
        definition, records, {"work_item", "execution_attempt"}, "context_bytes"
    ),
    "MET-007": _tokens,
    "MET-008": _quota,
    "MET-009": _usage_coverage,
    "MET-010": _delivery,
    "MET-011": _repetition,
}


def calculate_metrics(
    observations: list[dict[str, Any]],
    *,
    canonical_source_ids: set[str] | None = None,
    source_observation_semantics: dict[str, str] | None = None,
    usage_source_ids: set[str] | None = None,
    evaluation_time: str | None = None,
) -> list[dict[str, Any]]:
    records = latest_logical_records(
        observations,
        canonical_source_ids=canonical_source_ids,
        source_observation_semantics=source_observation_semantics,
    )
    return [
        (
            _accepted(item, records, evaluation_time=evaluation_time)
            if item["metric_id"] == "MET-001"
            else _tokens(item, records, usage_source_ids=usage_source_ids)
            if item["metric_id"] == "MET-007"
            else CALCULATORS[item["metric_id"]](item, records)
        )
        for item in METRIC_DEFINITIONS
    ]
