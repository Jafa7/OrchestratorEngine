"""Project-owned scope progress and deterministic effort forecasting."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from . import contracts
from .calculations import latest_logical_records
from .store import MetricsStore

PROJECT_OWNER_AUTHORITY = "project_owner"
ACTIVE_STATUSES = {
    "planned",
    "in_progress",
    "blocked",
    "review_ready",
    "accepted",
    "reopened",
}


def build_progress_report(
    store: MetricsStore,
    *,
    generation_digest: str | None = None,
    evaluation_time: str | None = None,
    baseline_revision: str | None = None,
    current_revision: str | None = None,
    module_id: str | None = None,
    minimum_samples: int = 5,
) -> dict[str, Any]:
    if minimum_samples < 1:
        raise ValueError("minimum_samples must be at least 1")
    generation = (
        store.generation(generation_digest, verify_objects=False)
        if generation_digest
        else store.current_generation(verify_objects=False)
    )
    cutoff = contracts.require_timestamp(
        evaluation_time or contracts.utc_now(), "evaluation_time"
    )
    cutoff_value = _parse_timestamp(cutoff)
    registry = store.registry(generation)
    enabled_sources = {
        item["source_id"]
        for item in registry["sources"]
        if item["enabled"] is True
    }
    project_sources = {
        item["source_id"]
        for item in registry["sources"]
        if item["enabled"] is True
        and item.get("authority") == PROJECT_OWNER_AUTHORITY
    }
    source_semantics = {
        item["source_id"]: item["observation_semantics"]
        for item in registry["sources"]
        if item["enabled"] is True
    }
    observations = [
        item
        for item in store.observations(generation)
        if item["source_id"] in enabled_sources
        and _parse_timestamp(item["known_at"]) <= cutoff_value
        and _parse_timestamp(item["effective_at"]) <= cutoff_value
    ]
    calculated = calculate_progress(
        observations,
        project_source_ids=project_sources,
        source_observation_semantics=source_semantics,
        baseline_revision=baseline_revision,
        current_revision=current_revision,
        module_id=module_id,
        minimum_samples=minimum_samples,
    )
    return {
        "schema_version": contracts.SCHEMA_VERSION,
        "kind": contracts.PROGRESS_KIND,
        "generation_digest": generation["generation_digest"],
        "registry_digest": generation["registry_digest"],
        "evaluation_time": cutoff,
        "registry_project_uuid": registry["project_uuid"],
        "module_id": module_id,
        "minimum_forecast_samples": minimum_samples,
        "authority": "advisory_only",
        **calculated,
    }


def calculate_progress(
    observations: list[dict[str, Any]],
    *,
    project_source_ids: set[str],
    source_observation_semantics: dict[str, str] | None = None,
    baseline_revision: str | None = None,
    current_revision: str | None = None,
    module_id: str | None = None,
    minimum_samples: int = 5,
) -> dict[str, Any]:
    if minimum_samples < 1:
        raise ValueError("minimum_samples must be at least 1")
    records = latest_logical_records(
        observations,
        source_observation_semantics=source_observation_semantics,
    )
    authoritative = [
        item for item in records if item["source_id"] in project_source_ids
    ]
    revisions = [
        item for item in authoritative if item["record_type"] == "scope_revision"
    ]
    if not project_source_ids:
        return _unavailable("project_owner_source_unavailable")
    if not revisions:
        return _unavailable("scope_revision_unavailable")

    selected_current = _select_revision(
        revisions, requested=current_revision, role="current"
    )
    if isinstance(selected_current, str):
        return _unavailable(selected_current)
    selected_baseline = _select_revision(
        revisions, requested=baseline_revision, role="baseline"
    )
    if isinstance(selected_baseline, str) and baseline_revision is not None:
        return _unavailable(selected_baseline)
    if isinstance(selected_baseline, str):
        selected_baseline = None

    all_scope_items = [
        item for item in authoritative if item["record_type"] == "scope_item"
    ]
    current_items, current_conflicts = _items_for_revision(
        all_scope_items,
        selected_current["data"]["scope_revision_id"],
        module_id=module_id,
    )
    if current_conflicts:
        return _unavailable(
            "conflicting_scope_items",
            details={"conflicting_work_item_ids": current_conflicts[:20]},
        )
    if not current_items:
        return _unavailable("current_scope_items_unavailable")
    baseline_items: list[dict[str, Any]] = []
    if selected_baseline is not None:
        baseline_items, baseline_conflicts = _items_for_revision(
            all_scope_items,
            selected_baseline["data"]["scope_revision_id"],
            module_id=module_id,
        )
        if baseline_conflicts:
            return _unavailable(
                "conflicting_scope_items",
                details={"conflicting_work_item_ids": baseline_conflicts[:20]},
            )

    acceptances = _acceptance_states(authoritative)
    current_summary = _scope_summary(current_items, acceptances)
    baseline_summary = (
        _scope_summary(baseline_items, acceptances) if baseline_items else None
    )
    scope_change = _scope_change(baseline_items, current_items)
    modules = _module_summaries(current_items, acceptances)
    forecast = _forecast(
        current_items=current_items,
        all_scope_items=all_scope_items,
        acceptances=acceptances,
        minimum_samples=minimum_samples,
    )
    return {
        "status": "available" if baseline_summary else "partial",
        "reason": "ok" if baseline_summary else "baseline_scope_unavailable",
        "baseline_revision": (
            selected_baseline["data"]["scope_revision_id"]
            if selected_baseline is not None
            else None
        ),
        "current_revision": selected_current["data"]["scope_revision_id"],
        "baseline": baseline_summary,
        "current": current_summary,
        "scope_change": scope_change,
        "modules": modules,
        "forecast": forecast,
        "details": {
            "accepted_requires": [
                "project_owner source",
                "explicit accepted=true observation",
                "matching scope_revision_id and criteria_revision",
                "non-empty evidence_ref or evidence_digest",
            ],
            "calendar_finish_date": None,
            "calendar_finish_reason": "scheduling_policy_not_supplied",
        },
    }


def markdown_progress(report: dict[str, Any]) -> str:
    lines = [
        "# OrchestratorEngine scope progress",
        "",
        f"Generation: `{report['generation_digest']}`",
        f"Evaluation time: `{report['evaluation_time']}`",
        f"Status: `{report['status']}` ({report['reason']})",
    ]
    current = report.get("current")
    if isinstance(current, dict):
        lines.extend(
            [
                "",
                f"Current revision: `{report['current_revision']}`",
                f"Accepted progress: {current['progress_percent']:.1f}% "
                f"({current['accepted_count']}/{current['item_count']} items)",
                f"Weighting: `{current['weighting_mode']}`",
            ]
        )
    change = report.get("scope_change")
    if isinstance(change, dict) and change.get("growth_percent") is not None:
        lines.append(f"Scope growth: {change['growth_percent']:+.1f}%")
    forecast = report.get("forecast", {})
    lines.extend(["", "## Forecast", ""])
    if forecast.get("status") == "available":
        lines.append(
            "Sequential equivalent effort: "
            f"P50 {forecast['sequential_effort_scenario_days_p50']:.2f} days, "
            f"P80 {forecast['sequential_effort_scenario_days_p80']:.2f} days."
        )
    else:
        lines.append(f"Unavailable: `{forecast.get('reason', 'unknown')}`.")
    lines.extend(
        [
            "",
            "Forecasts are deterministic advisory estimates, not calendar deadlines.",
            "",
        ]
    )
    return "\n".join(lines)


def _select_revision(
    revisions: list[dict[str, Any]], *, requested: str | None, role: str
) -> dict[str, Any] | str:
    if requested is not None:
        matches = [
            item
            for item in revisions
            if item["data"].get("scope_revision_id") == requested
        ]
        if len(matches) != 1:
            return f"{role}_scope_revision_unavailable"
        return matches[0]
    if role == "baseline":
        candidates = [item for item in revisions if item["data"].get("baseline")]
        if not candidates:
            return "baseline_scope_unavailable"
        if len({item["data"]["scope_revision_id"] for item in candidates}) != 1:
            return "ambiguous_baseline_scope_revision"
        return min(candidates, key=_revision_order)
    candidates = [item for item in revisions if item["data"].get("current")]
    if not candidates:
        candidates = revisions
    highest = max(item["data"]["revision_index"] for item in candidates)
    matches = [
        item for item in candidates if item["data"]["revision_index"] == highest
    ]
    if len(matches) != 1:
        return "ambiguous_current_scope_revision"
    return matches[0]


def _revision_order(item: dict[str, Any]) -> tuple[int, float, str]:
    return (
        item["data"]["revision_index"],
        _parse_timestamp(item["known_at"]).timestamp(),
        item["observation_id"],
    )


def _items_for_revision(
    records: list[dict[str, Any]], revision_id: str, *, module_id: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        data = item["data"]
        if data.get("scope_revision_id") != revision_id:
            continue
        if module_id is not None and data.get("module_id") != module_id:
            continue
        grouped[data["work_item_id"]].append(item)
    conflicts: list[str] = []
    selected: list[dict[str, Any]] = []
    for work_item_id, items in grouped.items():
        sources = {item["source_id"] for item in items}
        payloads = {contracts.content_digest(item["data"]) for item in items}
        if len(sources) > 1 and len(payloads) > 1:
            conflicts.append(work_item_id)
            continue
        selected.append(max(items, key=_record_order))
    return sorted(selected, key=lambda item: item["data"]["work_item_id"]), sorted(
        conflicts
    )


def _acceptance_states(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        if item["record_type"] != "acceptance":
            continue
        data = item["data"]
        key = (
            data.get("work_item_id"),
            data.get("scope_revision_id"),
            data.get("criteria_revision"),
        )
        if all(isinstance(value, str) and value for value in key):
            grouped[key].append(item)
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for identity, items in grouped.items():
        latest_by_source: dict[str, dict[str, Any]] = {}
        for item in sorted(items, key=_record_order):
            latest_by_source[item["source_id"]] = item
        current = list(latest_by_source.values())
        unaccepted = [item for item in current if not _is_accepted(item)]
        selected[identity] = max(unaccepted or current, key=_record_order)
    return selected


def _record_order(item: dict[str, Any]) -> tuple[float, float, str]:
    return (
        _parse_timestamp(item["known_at"]).timestamp(),
        _parse_timestamp(item["effective_at"]).timestamp(),
        item["observation_id"],
    )


def _acceptance_for_item(
    item: dict[str, Any],
    acceptances: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    data = item["data"]
    return acceptances.get(
        (
            data["work_item_id"],
            data["scope_revision_id"],
            data["criteria_revision"],
        )
    )


def _is_accepted(item: dict[str, Any] | None) -> bool:
    if item is None or item["data"].get("accepted") is not True:
        return False
    data = item["data"]
    return any(
        isinstance(data.get(field), str) and data[field]
        for field in ("evidence_ref", "evidence_digest")
    )


def _weight(item: dict[str, Any]) -> float:
    value = item["data"].get("weight")
    return float(value) if isinstance(value, (int, float)) else 1.0


def _scope_summary(
    items: list[dict[str, Any]],
    acceptances: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    active = [item for item in items if item["data"]["status"] != "removed"]
    accepted = [
        item
        for item in active
        if item["data"]["status"] != "reopened"
        and _is_accepted(_acceptance_for_item(item, acceptances))
    ]
    total_weight = sum(_weight(item) for item in active)
    accepted_weight = sum(_weight(item) for item in accepted)
    explicit_weights = sum("weight" in item["data"] for item in active)
    if explicit_weights == len(active):
        weighting_mode = "owner_supplied"
    elif explicit_weights == 0:
        weighting_mode = "uniform_items"
    else:
        weighting_mode = "mixed_explicit_and_uniform"
    ratio = accepted_weight / total_weight if total_weight else 0.0
    statuses = Counter(item["data"]["status"] for item in active)
    return {
        "item_count": len(active),
        "accepted_count": len(accepted),
        "accepted_weight": accepted_weight,
        "denominator_weight": total_weight,
        "progress_ratio": ratio,
        "progress_percent": ratio * 100,
        "weighting_mode": weighting_mode,
        "status_distribution": {
            status: statuses.get(status, 0) for status in sorted(ACTIVE_STATUSES)
        },
        "accepted_without_evidence_count": sum(
            1
            for item in active
            if item["data"]["status"] == "accepted"
            and not _is_accepted(_acceptance_for_item(item, acceptances))
        ),
    }


def _scope_change(
    baseline_items: list[dict[str, Any]], current_items: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not baseline_items:
        return None
    baseline = {
        item["data"]["work_item_id"]: item
        for item in baseline_items
        if item["data"]["status"] != "removed"
    }
    current = {
        item["data"]["work_item_id"]: item
        for item in current_items
        if item["data"]["status"] != "removed"
    }
    current_all = {
        item["data"]["work_item_id"]: item for item in current_items
    }
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    carried = sorted(set(baseline) & set(current))
    baseline_weight = sum(_weight(item) for item in baseline.values())
    current_weight = sum(_weight(item) for item in current.values())
    delta = current_weight - baseline_weight
    reasons = sorted(
        {
            str(current[item]["data"].get("change_reason"))
            for item in added
            if current[item]["data"].get("change_reason")
        }
        | {
            str(
                current_all.get(item, baseline[item])["data"].get("change_reason")
            )
            for item in removed
            if current_all.get(item, baseline[item])["data"].get("change_reason")
        }
    )[:20]
    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "carried_count": len(carried),
        "split_count": sum(
            1
            for item in current.values()
            if set(item["data"].get("parent_work_item_ids", [])) & set(baseline)
        ),
        "added_work_item_ids": added[:50],
        "removed_work_item_ids": removed[:50],
        "baseline_weight": baseline_weight,
        "current_weight": current_weight,
        "growth_weight": delta,
        "growth_ratio": delta / baseline_weight if baseline_weight else None,
        "growth_percent": (
            delta * 100 / baseline_weight if baseline_weight else None
        ),
        "change_reasons": reasons,
    }


def _module_summaries(
    current_items: list[dict[str, Any]],
    acceptances: dict[tuple[str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in current_items:
        grouped[str(item["data"].get("module_id", "unassigned"))].append(item)
    return [
        {"module_id": module_id, **_scope_summary(items, acceptances)}
        for module_id, items in sorted(grouped.items())
    ]


def _forecast(
    *,
    current_items: list[dict[str, Any]],
    all_scope_items: list[dict[str, Any]],
    acceptances: dict[tuple[str, str, str], dict[str, Any]],
    minimum_samples: int,
) -> dict[str, Any]:
    current_active = [
        item for item in current_items if item["data"]["status"] != "removed"
    ]
    remaining = [
        item
        for item in current_active
        if item["data"]["status"] == "reopened"
        or not _is_accepted(_acceptance_for_item(item, acceptances))
    ]
    if not remaining:
        return {
            "status": "available",
            "reason": "scope_complete",
            "evidence_class": "estimated",
            "mode": "sequential_equivalent_effort",
            "remaining_item_count": 0,
            "sequential_effort_scenario_days_p50": 0.0,
            "sequential_effort_scenario_days_p80": 0.0,
            "blocked_days_p50": 0.0,
            "blocked_days_p80": 0.0,
            "sample_count": 0,
            "cohorts": [],
        }
    missing_classes = sorted(
        {
            item["data"]["work_item_id"]
            for item in remaining
            if not isinstance(item["data"].get("work_class"), str)
            or not item["data"]["work_class"]
        }
    )
    if missing_classes:
        return _forecast_unavailable(
            "comparable_work_class_missing",
            remaining=len(remaining),
            details={"work_item_ids": missing_classes[:20]},
        )

    unique_historical: dict[
        str, tuple[dict[str, Any], dict[str, Any]]
    ] = {}
    conflicting_cycles: set[str] = set()
    for item in all_scope_items:
        data = item["data"]
        identity = (
            data["work_item_id"],
            data["scope_revision_id"],
            data["criteria_revision"],
        )
        accepted = acceptances.get(identity)
        if not _is_accepted(accepted):
            continue
        if data["status"] == "reopened":
            continue
        cycle_id = accepted["data"].get("completion_cycle_id")
        if not isinstance(cycle_id, str) or not cycle_id:
            continue
        current = unique_historical.get(cycle_id)
        candidate = (item, accepted)
        if current is None:
            unique_historical[cycle_id] = candidate
        elif _cycle_signature(current) != _cycle_signature(candidate):
            conflicting_cycles.add(cycle_id)
    for cycle_id in conflicting_cycles:
        unique_historical.pop(cycle_id, None)
    samples: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for item, acceptance in unique_historical.values():
        data = item["data"]
        work_class = data.get("work_class")
        start = _optional_timestamp(data.get("started_at"))
        end = _optional_timestamp(
            acceptance["data"].get("accepted_at") or acceptance["effective_at"]
        )
        if (
            not isinstance(work_class, str)
            or start is None
            or end is None
            or end < start
        ):
            continue
        blocked = data.get("blocked_seconds", 0)
        blocked_seconds = (
            float(blocked)
            if isinstance(blocked, (int, float)) and not isinstance(blocked, bool)
            else 0.0
        )
        elapsed = end - start
        blocked_seconds = min(elapsed, max(0.0, blocked_seconds))
        unit_weight = _weight(item)
        samples[work_class].append(
            (
                max(0.0, elapsed - blocked_seconds) / unit_weight,
                blocked_seconds / unit_weight,
                elapsed / unit_weight,
            )
        )

    cohorts: list[dict[str, Any]] = []
    missing: dict[str, int] = {}
    active_p50 = active_p80 = blocked_p50 = blocked_p80 = 0.0
    total_p50 = total_p80 = 0.0
    for work_class in sorted({item["data"]["work_class"] for item in remaining}):
        cohort_samples = samples.get(work_class, [])
        if len(cohort_samples) < minimum_samples:
            missing[work_class] = len(cohort_samples)
            continue
        class_items = [
            item for item in remaining if item["data"]["work_class"] == work_class
        ]
        remaining_weight = sum(_weight(item) for item in class_items)
        active_values = [sample[0] for sample in cohort_samples]
        blocked_values = [sample[1] for sample in cohort_samples]
        total_values = [sample[2] for sample in cohort_samples]
        class_active_p50 = _quantile(active_values, 0.5) * remaining_weight
        class_active_p80 = _quantile(active_values, 0.8) * remaining_weight
        class_blocked_p50 = _quantile(blocked_values, 0.5) * remaining_weight
        class_blocked_p80 = _quantile(blocked_values, 0.8) * remaining_weight
        class_total_p50 = _quantile(total_values, 0.5) * remaining_weight
        class_total_p80 = _quantile(total_values, 0.8) * remaining_weight
        active_p50 += class_active_p50
        active_p80 += class_active_p80
        blocked_p50 += class_blocked_p50
        blocked_p80 += class_blocked_p80
        total_p50 += class_total_p50
        total_p80 += class_total_p80
        cohorts.append(
            {
                "work_class": work_class,
                "sample_count": len(cohort_samples),
                "remaining_item_count": len(class_items),
                "remaining_weight": remaining_weight,
                "active_effort_days_p50": class_active_p50 / 86400,
                "active_effort_days_p80": class_active_p80 / 86400,
                "blocked_days_p50": class_blocked_p50 / 86400,
                "blocked_days_p80": class_blocked_p80 / 86400,
                "total_cycle_days_p50": class_total_p50 / 86400,
                "total_cycle_days_p80": class_total_p80 / 86400,
            }
        )
    if missing:
        return _forecast_unavailable(
            "insufficient_comparable_history",
            remaining=len(remaining),
            details={
                "minimum_samples": minimum_samples,
                "available_samples_by_work_class": missing,
            },
        )
    known_ids = {item["data"]["work_item_id"] for item in current_active}
    dependency_total = sum(
        len(item["data"].get("dependency_ids", [])) for item in remaining
    )
    dependency_known = sum(
        1
        for item in remaining
        for dependency in item["data"].get("dependency_ids", [])
        if dependency in known_ids
    )
    return {
        "status": "available",
        "reason": "ok",
        "evidence_class": "estimated",
        "mode": "sequential_equivalent_effort",
        "remaining_item_count": len(remaining),
        "sequential_effort_scenario_days_p50": total_p50 / 86400,
        "sequential_effort_scenario_days_p80": total_p80 / 86400,
        "aggregation_semantics": "sum_of_work_class_cycle_scenarios",
        "active_effort_days_p50": active_p50 / 86400,
        "active_effort_days_p80": active_p80 / 86400,
        "blocked_days_p50": blocked_p50 / 86400,
        "blocked_days_p80": blocked_p80 / 86400,
        "sample_count": sum(item["sample_count"] for item in cohorts),
        "conflicting_completion_cycle_count": len(conflicting_cycles),
        "cohorts": cohorts,
        "dependency_coverage": {
            "known": dependency_known,
            "total": dependency_total,
            "complete": dependency_known == dependency_total,
        },
        "calendar_finish_date": None,
        "calendar_finish_reason": "parallelism_and_work_calendar_not_supplied",
    }


def _cycle_signature(
    value: tuple[dict[str, Any], dict[str, Any]],
) -> tuple[object, ...]:
    item, acceptance = value
    return (
        item["data"].get("work_class"),
        item["data"].get("started_at"),
        item["data"].get("blocked_seconds", 0),
        _weight(item),
        acceptance["data"].get("accepted_at") or acceptance["effective_at"],
        acceptance["data"].get("evidence_digest")
        or acceptance["data"].get("evidence_ref"),
    )


def _forecast_unavailable(
    reason: str, *, remaining: int, details: dict[str, Any]
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "evidence_class": "unknown",
        "mode": "sequential_equivalent_effort",
        "remaining_item_count": remaining,
        "sequential_effort_scenario_days_p50": None,
        "sequential_effort_scenario_days_p80": None,
        "details": details,
    }


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _optional_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_timestamp(value).timestamp()
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _unavailable(
    reason: str, *, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "baseline_revision": None,
        "current_revision": None,
        "baseline": None,
        "current": None,
        "scope_change": None,
        "modules": [],
        "forecast": _forecast_unavailable(
            "scope_progress_unavailable", remaining=0, details={}
        ),
        "details": details or {},
    }
