"""Pinned JSON reports and equivalent Markdown rendering."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import contracts
from .calculations import calculate_metrics
from .catalog import METRIC_DEFINITIONS
from .store import MetricsStore


def build_report(
    store: MetricsStore,
    *,
    generation_digest: str | None = None,
    evaluation_time: str | None = None,
    package_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    generation = (
        store.generation(generation_digest, verify_objects=False)
        if generation_digest
        else store.current_generation(verify_objects=False)
    )
    cutoff = contracts.require_timestamp(
        evaluation_time or contracts.utc_now(), "evaluation_time"
    )
    cutoff_value = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    registry = store.registry(generation)
    enabled_sources = {
        item["source_id"]
        for item in registry["sources"]
        if item["enabled"] is True
    }
    canonical_sources = {
        item["source_id"]
        for item in registry["sources"]
        if item["enabled"] is True
        and item.get("identity_mapping") == "canonical_identity_authorized"
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
        if datetime.fromisoformat(item["known_at"].replace("Z", "+00:00"))
        <= cutoff_value
        and datetime.fromisoformat(item["effective_at"].replace("Z", "+00:00"))
        <= cutoff_value
        and (
            package_id is None
            or item["scope"].get("package_id") == package_id
            or item["data"].get("package_id") == package_id
        )
        and (
            operation_id is None
            or item["scope"].get("operation_id") == operation_id
            or item["data"].get("operation_id") == operation_id
        )
    ]
    return {
        "schema_version": contracts.SCHEMA_VERSION,
        "kind": contracts.REPORT_KIND,
        "generation_digest": generation["generation_digest"],
        "registry_digest": generation["registry_digest"],
        "evaluation_time": cutoff,
        "event_cutoff": cutoff,
        "knowledge_cutoff": cutoff,
        "registry_project_uuid": registry["project_uuid"],
        "policy_id": None,
        "cohort": {
            "package_id": package_id,
            "operation_id": operation_id,
        },
        "formula_catalog_version": 1,
        "formula_catalog_digest": contracts.content_digest(METRIC_DEFINITIONS),
        "access": "local",
        "observation_count": len(observations),
        "metrics": calculate_metrics(
            observations,
            canonical_source_ids=canonical_sources,
            source_observation_semantics=source_semantics,
            evaluation_time=cutoff,
        ),
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# OrchestratorEngine metrics",
        "",
        f"Generation: `{report['generation_digest']}`",
        f"Evaluation time: `{report['evaluation_time']}`",
        f"Observations: {report['observation_count']}",
        "",
        "| Metric | Value | Unit | Availability | Coverage |",
        "|---|---:|---|---|---:|",
    ]
    for metric in report["metrics"]:
        value = metric["value"]
        if isinstance(value, float):
            rendered = f"{value:.4g}"
        elif value is None:
            rendered = "unknown"
        else:
            rendered = str(value)
        coverage = metric["coverage"]
        lines.append(
            f"| {metric['metric_id']} {metric['name']} | {rendered} | "
            f"{metric['unit']} | {metric['availability']} | "
            f"{coverage['observed']}/{coverage['total']} |"
        )
    lines.extend(
        [
            "",
            "Provider usage and quota values are reported only when a registered "
            "source supplied them. Bytes are never converted to tokens.",
            "",
        ]
    )
    return "\n".join(lines)


def compare_reports(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    compatibility_fields = (
        "registry_project_uuid",
        "evaluation_time",
        "policy_id",
        "cohort",
        "formula_catalog_digest",
        "access",
    )
    incompatible_fields = [
        field
        for field in compatibility_fields
        if baseline.get(field) != candidate.get(field)
    ]
    compatible = not incompatible_fields
    baseline_values = {item["metric_id"]: item for item in baseline["metrics"]}
    comparisons: list[dict[str, Any]] = []
    for item in candidate["metrics"]:
        previous = baseline_values[item["metric_id"]]
        before = previous["value"]
        after = item["value"]
        delta: float | None = None
        if (
            compatible
            and previous.get("unit") == item.get("unit")
            and isinstance(before, (int, float))
            and isinstance(after, (int, float))
        ):
            delta = float(after) - float(before)
        comparisons.append(
            {
                "metric_id": item["metric_id"],
                "baseline": before,
                "candidate": after,
                "delta": delta,
                "comparable": delta is not None,
            }
        )
    return {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_METRICS_COMPARISON",
        "status": "comparable" if compatible else "incompatible",
        "baseline_generation": baseline["generation_digest"],
        "candidate_generation": candidate["generation_digest"],
        "incompatible_fields": incompatible_fields,
        "metrics": comparisons,
    }
