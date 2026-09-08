"""Deterministic, advisory-only next-action guidance."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import contracts
from .calculations import latest_logical_records


def advise(
    observations: list[dict[str, Any]],
    *,
    scope: str,
    package_id: str | None = None,
    operation_id: str | None = None,
    generation_digest: str | None = None,
    project_owner_source_ids: set[str] | None = None,
    canonical_source_ids: set[str] | None = None,
    source_observation_semantics: dict[str, str] | None = None,
    evaluation_time: str | None = None,
) -> dict[str, Any]:
    if scope not in {"package", "operation_only"}:
        raise ValueError("scope must be package or operation_only")
    as_of = contracts.require_timestamp(
        evaluation_time or contracts.utc_now(), "evaluation_time"
    )
    if scope == "package" and not package_id:
        return _guidance(
            scope=scope,
            status="unavailable",
            next_action="use_ordinary_project_policy",
            reason="package_context_unavailable",
            pending=[],
            as_of=as_of,
        )
    cutoff = _timestamp(as_of)
    records = latest_logical_records(
        [
            item
            for item in observations
            if _timestamp(item["known_at"]) <= cutoff
            and _timestamp(item["effective_at"]) <= cutoff
            if (
                package_id is None
                or item["scope"].get("package_id") == package_id
                or item["data"].get("package_id") == package_id
            )
            and (
                operation_id is None
                or item["scope"].get("operation_id") == operation_id
                or item["data"].get("operation_id") == operation_id
            )
        ],
        # Canonical identity proves object equivalence, not authority. Keep
        # source envelopes separate here so complement fields cannot inherit a
        # different source's project-owner authority.
        canonical_source_ids=None,
        source_observation_semantics=source_observation_semantics,
    )
    owner_sources = project_owner_source_ids or set()
    current_binding, binding_reason = _select_package_binding(
        records, project_owner_source_ids=owner_sources
    )
    if scope == "package" and current_binding is None:
        return _guidance(
            scope=scope,
            status="unavailable",
            next_action="use_ordinary_project_policy",
            reason=binding_reason,
            pending=[],
            as_of=as_of,
        )
    pending = _pending_obligations(
        records,
        current_binding=current_binding,
        project_owner_source_ids=owner_sources,
    )
    attempts = [item for item in records if item["record_type"] == "execution_attempt"]
    if scope == "operation_only" and not records:
        return _guidance(
            scope=scope,
            status="unavailable",
            next_action="use_ordinary_project_policy",
            reason="operation_context_unavailable",
            pending=[],
            as_of=as_of,
        )
    lifecycle_attempts = [
        item
        for item in attempts
        if isinstance(item["data"].get("status"), str)
        and (
            current_binding is None
            or _attempt_applies_to_binding(item["data"], current_binding)
        )
    ]
    current_attempts, ambiguous_operations = _current_operation_attempts(
        lifecycle_attempts,
        canonical_source_ids=canonical_source_ids or set(),
    )
    running = [
        item
        for item in current_attempts
        if item["data"].get("status") in {"starting", "queued", "pending", "running"}
    ]
    incomplete = [
        item
        for item in current_attempts
        if item["data"].get("status")
        not in {
            "completed",
            "passed",
            "succeeded",
            "failed",
            "errored",
            "timed_out",
        }
    ]
    failed = [
        item
        for item in attempts
        if item["data"].get("status") in {"failed", "errored", "timed_out"}
    ]
    conflicts = [
        item
        for item in records
        if item["record_type"] == "source_capability"
        and item["data"].get("status") in {"unavailable", "rate_limited"}
    ]
    equivalent = [
        item
        for item in attempts
        if item["data"].get("equivalent_repetition") is True
        or (
            item["data"].get("equivalence_key")
            and item["data"].get("evidence_delta") is False
        )
    ]
    historical_final_gate_passed = _final_gate_passed(
        attempts,
        owner_sources=owner_sources,
        current_binding=current_binding,
    )
    final_gate_passed = _final_gate_passed(
        current_attempts,
        owner_sources=owner_sources,
        current_binding=current_binding,
    )
    reuse = [
        item["data"]
        for item in records
        if item["record_type"] == "classification"
        and item["data"].get("classification_kind") == "reuse_decision"
    ]
    invalid_reuse = [
        item
        for item in reuse
        if item.get("status") in {"invalid", "unknown"}
        or item.get("invalidation_reason")
        or not _reuse_bindings_complete(item)
        or not _matches_package_binding(item, current_binding)
        or not _source_execution_exists(item, attempts)
    ]
    foreign_mutations = [
        item["data"]
        for item in records
        if item["record_type"] == "classification"
        and item["data"].get("classification_kind") == "environment_mutation"
        and item["data"].get("planned") is False
    ]
    invalidating_mutations = [
        item["data"]
        for item in records
        if item["record_type"] == "classification"
        and item["data"].get("classification_kind") == "environment_mutation"
        and item["data"].get("invalidates_live_readiness") is True
    ]
    unresolved_failures = [
        item
        for item in failed
        if _failure_is_unresolved(
            item,
            records,
            current_binding=current_binding,
            project_owner_source_ids=owner_sources,
        )
    ]
    if running:
        action, reason = "wait_for_running_operation", "operation_in_flight"
    elif conflicts:
        action, reason = "use_project_outage_fallback", "resource_unavailable"
    elif foreign_mutations:
        action, reason = "restore_or_revalidate_environment", "foreign_mutation"
    elif invalidating_mutations:
        action, reason = "revalidate_environment", "planned_state_transition"
    elif invalid_reuse:
        action, reason = "refresh_invalidated_evidence", "reuse_not_applicable"
    elif len(equivalent) >= 2:
        action, reason = "diagnose_before_repeating", "equivalent_repetition"
    elif unresolved_failures:
        action, reason = "repair_failed_operation", "failed_evidence"
    elif incomplete or ambiguous_operations:
        action, reason = (
            "inspect_incomplete_operation",
            (
                "operation_attempt_ambiguous"
                if ambiguous_operations
                else "operation_terminal_outcome_unavailable"
            ),
        )
    elif scope == "operation_only" and current_attempts and all(
        item["data"].get("status") in {"completed", "passed", "succeeded"}
        for item in current_attempts
    ):
        action, reason = "handoff_operation_result", "operation_evidence_complete"
    elif scope == "operation_only":
        action, reason = (
            "inspect_incomplete_operation",
            "operation_terminal_outcome_unavailable",
        )
    elif pending:
        action, reason = "complete_next_pending_obligation", "pending_obligations"
    elif not final_gate_passed:
        action, reason = "run_project_required_final_gate", "final_gate_not_proven"
    else:
        action, reason = "handoff_for_acceptance", "project_evidence_complete"
    return _guidance(
        scope=scope,
        status="available",
        next_action=action,
        reason=reason,
        pending=pending,
        details={
            "check_required": bool(
                unresolved_failures
                or incomplete
                or ambiguous_operations
                or (scope == "package" and (pending or not final_gate_passed))
            ),
            "check_admissible_now": not bool(running or conflicts),
            "historical_verification": historical_final_gate_passed,
            "live_readiness": not bool(
                running
                or incomplete
                or ambiguous_operations
                or conflicts
                or invalidating_mutations
            ),
            "publication_eligible": (
                scope == "package"
                and final_gate_passed
                and not running
                and not incomplete
                and not ambiguous_operations
                and not conflicts
                and not unresolved_failures
                and not pending
                and not foreign_mutations
                and not invalidating_mutations
                and not invalid_reuse
            ),
            "reuse_decisions": len(reuse),
            "invalid_reuse_decisions": len(invalid_reuse),
            "unresolved_failure_count": len(unresolved_failures),
            "running_operation_ids": sorted(
                {
                    str(item["data"].get("operation_id"))
                    for item in running
                    if item["data"].get("operation_id")
                }
            )[:20],
            "incomplete_operation_ids": sorted(
                {
                    str(item["data"].get("operation_id"))
                    for item in incomplete
                    if item["data"].get("operation_id")
                }
            )[:20],
            "ambiguous_operation_ids": ambiguous_operations[:20],
            "resource_conflict_sources": sorted(
                {item["source_id"] for item in conflicts}
            )[:20],
            "package_binding": current_binding,
            "generation_digest": generation_digest,
            "evaluation_time": as_of,
            "stop_condition": (
                "return control after the recommended action produces durable evidence"
            ),
        },
        as_of=as_of,
    )


def _final_gate_passed(
    attempts: list[dict[str, Any]],
    *,
    owner_sources: set[str],
    current_binding: dict[str, Any] | None,
) -> bool:
    return any(
        item["data"].get("verification_role") == "final_gate"
        and item["data"].get("status") == "completed"
        and item["data"].get("accepted") is True
        and item["data"].get("authority_status") == "owner_authorized"
        and item["source_id"] in owner_sources
        and all(
            isinstance(item["data"].get(field), str) and item["data"][field]
            for field in ("candidate_id", "check_plan_revision", "evidence_digest")
        )
        and _matches_package_binding(item["data"], current_binding)
        for item in attempts
    )


def _reuse_bindings_complete(value: dict[str, Any]) -> bool:
    required = {
        "requirement_id",
        "check_plan_revision",
        "candidate_id",
        "source_execution_id",
        "environment_id",
        "evidence_digest",
        "policy_revision",
    }
    return all(isinstance(value.get(field), str) and value[field] for field in required)


def _package_bindings_complete(value: dict[str, Any]) -> bool:
    required = {
        "requirement_set_id",
        "candidate_id",
        "check_plan_revision",
        "policy_revision",
    }
    obligation_ids = value.get("obligation_ids")
    return (
        all(isinstance(value.get(field), str) and value[field] for field in required)
        and value.get("obligations_complete") is True
        and isinstance(obligation_ids, list)
        and all(isinstance(item, str) and item for item in obligation_ids)
        and len(obligation_ids) == len(set(obligation_ids))
    )


def _select_package_binding(
    records: list[dict[str, Any]], *, project_owner_source_ids: set[str]
) -> tuple[dict[str, Any] | None, str]:
    latest_by_source: dict[str, dict[str, Any]] = {}
    for item in sorted(records, key=_applicability_order):
        if (
            item["record_type"] == "classification"
            and item["data"].get("classification_kind") == "package_binding"
            and item["source_id"] in project_owner_source_ids
        ):
            latest_by_source[item["source_id"]] = item
    if not latest_by_source:
        return None, "package_context_unavailable"
    signatures = {
        _binding_signature(item["data"]) for item in latest_by_source.values()
    }
    if len(signatures) != 1:
        return None, "ambiguous_package_binding"
    current = max(latest_by_source.values(), key=_applicability_order)["data"]
    if not _package_bindings_complete(current):
        return None, "package_binding_incomplete"
    return current, "ok"


def _binding_signature(value: dict[str, Any]) -> tuple[object, ...]:
    return (
        value.get("requirement_set_id"),
        tuple(sorted(value.get("requirement_ids", []))),
        value.get("candidate_id"),
        value.get("check_plan_revision"),
        value.get("policy_revision"),
        tuple(sorted(value.get("obligation_ids", []))),
        value.get("obligations_complete"),
    )


def _pending_obligations(
    records: list[dict[str, Any]],
    *,
    current_binding: dict[str, Any] | None,
    project_owner_source_ids: set[str],
) -> list[str]:
    if current_binding is None:
        return []
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for item in sorted(records, key=_record_order):
        obligation_id = item["data"].get("obligation_id")
        if (
            item["record_type"] == "obligation"
            and item["source_id"] in project_owner_source_ids
            and isinstance(obligation_id, str)
            and obligation_id
        ):
            grouped.setdefault(obligation_id, {})[item["source_id"]] = item
    pending = []
    for obligation_id in current_binding.get("obligation_ids", []):
        states = list(grouped.get(obligation_id, {}).values())
        if not states or any(
            item["data"].get("status") not in {"completed", "waived"}
            or not _obligation_matches_binding(item["data"], current_binding)
            for item in states
        ):
            pending.append(obligation_id)
    return pending


def _obligation_matches_binding(
    value: dict[str, Any], binding: dict[str, Any]
) -> bool:
    return all(
        value.get(field) == binding.get(field)
        for field in (
            "requirement_set_id",
            "candidate_id",
            "check_plan_revision",
            "policy_revision",
        )
    )


def _current_operation_attempts(
    attempts: list[dict[str, Any]],
    *,
    canonical_source_ids: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    labels: dict[tuple[str, ...], str] = {}
    unidentified: set[str] = set()
    for item in attempts:
        key, label = _operation_key(
            item, canonical_source_ids=canonical_source_ids
        )
        if key is not None:
            grouped.setdefault(key, []).append(item)
            labels[key] = label
        else:
            label = f"unidentified:{item['observation_id']}"
            key = ("unidentified", item["source_id"], item["observation_id"])
            grouped[key] = [item]
            labels[key] = label
            unidentified.add(label)
    selected: list[dict[str, Any]] = []
    ambiguous = list(unidentified)
    for key, items in grouped.items():
        latest, is_ambiguous = _latest_attempt(items)
        if is_ambiguous:
            ambiguous.append(labels[key])
        selected.append(max(latest, key=_record_order))
    return selected, sorted(ambiguous)


def _operation_key(
    item: dict[str, Any], *, canonical_source_ids: set[str]
) -> tuple[tuple[str, ...] | None, str]:
    data = item["data"]
    canonical = data.get("canonical_identity")
    if item["source_id"] in canonical_source_ids and isinstance(canonical, dict):
        namespace = canonical.get("namespace")
        value = canonical.get("id")
        if (
            isinstance(namespace, str)
            and namespace
            and isinstance(value, str)
            and value
        ):
            return ("canonical", namespace, value), f"{namespace}:{value}"
    operation_id = data.get("operation_id")
    execution_id = data.get("execution_id")
    identifier = (
        operation_id
        if isinstance(operation_id, str) and operation_id
        else execution_id
    )
    if not isinstance(identifier, str) or not identifier:
        return None, "unidentified"
    operation_kind = (
        data.get("operation_kind") or data.get("execution_kind") or "native"
    )
    label = f"{item['source_id']}:{operation_kind}:{identifier}"
    return ("native", item["source_id"], str(operation_kind), identifier), label


def _latest_attempt(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    sequences = [item["data"].get("attempt_sequence") for item in items]
    valid_sequences = [
        value
        for value in sequences
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if valid_sequences:
        if len(valid_sequences) != len(items):
            return items, True
        latest_sequence = max(valid_sequences)
        latest = [
            item
            for item in items
            if item["data"].get("attempt_sequence") == latest_sequence
        ]
        return latest, len(latest) != 1
    starts = [_optional_timestamp(item["data"].get("started_at")) for item in items]
    known_starts = [value for value in starts if value is not None]
    if known_starts:
        if len(known_starts) != len(items):
            return items, True
        latest_start = max(known_starts)
        latest = [
            item
            for item, value in zip(items, starts, strict=True)
            if value == latest_start
        ]
        return latest, len(latest) != 1
    effective = [_timestamp(item["effective_at"]) for item in items]
    latest_effective = max(effective)
    latest = [
        item
        for item, value in zip(items, effective, strict=True)
        if value == latest_effective
    ]
    return latest, len(latest) != 1


def _matches_package_binding(
    value: dict[str, Any], binding: dict[str, Any] | None
) -> bool:
    if binding is None:
        return True
    for field in ("candidate_id", "check_plan_revision", "policy_revision"):
        if value.get(field) != binding.get(field):
            return False
    requirement_ids = binding.get("requirement_ids")
    if isinstance(requirement_ids, list) and value.get("requirement_id") is not None:
        return value["requirement_id"] in requirement_ids
    return True


def _attempt_applies_to_binding(
    value: dict[str, Any], binding: dict[str, Any]
) -> bool:
    return all(
        value.get(field) is None or value.get(field) == binding.get(field)
        for field in ("candidate_id", "check_plan_revision", "policy_revision")
    )


def _source_execution_exists(
    value: dict[str, Any], attempts: list[dict[str, Any]]
) -> bool:
    source_execution_id = value.get("source_execution_id")
    candidates = [
        item
        for item in attempts
        if item["data"].get("execution_id") == source_execution_id
    ]
    source_id = value.get("source_execution_source_id")
    if isinstance(source_id, str) and source_id:
        return any(item["source_id"] == source_id for item in candidates)
    return len({item["source_id"] for item in candidates}) == 1


def _failure_is_unresolved(
    failure: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    current_binding: dict[str, Any] | None,
    project_owner_source_ids: set[str],
) -> bool:
    data = failure["data"]
    if failure["source_id"] in project_owner_source_ids and data.get("disposition") in {
        "resolved",
        "superseded",
        "accepted_risk",
        "not_applicable",
    }:
        return False
    if (
        current_binding is not None
        and data.get("candidate_id") is not None
        and not _matches_package_binding(data, current_binding)
    ):
        return False
    execution_id = data.get("execution_id")
    dispositions = [
        item
        for item in records
        if item["record_type"] == "classification"
        and item["data"].get("classification_kind") == "failure_disposition"
        and item["data"].get("source_execution_id") == execution_id
        and item["data"].get("status")
        in {"resolved", "superseded", "accepted_risk", "not_applicable"}
        and item["source_id"] in project_owner_source_ids
        and _targets_failure(item, failure, records)
    ]
    if data.get("candidate_id") is not None:
        dispositions = [
            item
            for item in dispositions
            if item["data"].get("candidate_id") in {None, data.get("candidate_id")}
        ]
    return not dispositions


def _targets_failure(
    disposition: dict[str, Any],
    failure: dict[str, Any],
    records: list[dict[str, Any]],
) -> bool:
    source_id = disposition["data"].get("source_execution_source_id")
    if isinstance(source_id, str) and source_id:
        return source_id == failure["source_id"]
    execution_id = failure["data"].get("execution_id")
    matching_sources = {
        item["source_id"]
        for item in records
        if item["record_type"] == "execution_attempt"
        and item["data"].get("execution_id") == execution_id
    }
    return matching_sources == {failure["source_id"]}


def _record_order(item: dict[str, Any]) -> tuple[float, float, str]:
    return (
        _timestamp(item["known_at"]),
        _timestamp(item["effective_at"]),
        item["observation_id"],
    )


def _applicability_order(item: dict[str, Any]) -> tuple[float, float, str]:
    return (
        _timestamp(item["effective_at"]),
        _timestamp(item["known_at"]),
        item["observation_id"],
    )


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _optional_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return _timestamp(value)
    except ValueError:
        return None


def outage_guidance(*, scope: str, reason: str) -> dict[str, Any]:
    return _guidance(
        scope=scope,
        status="unavailable",
        next_action="use_ordinary_project_policy",
        reason=reason,
        pending=[],
    )


def _guidance(
    *,
    scope: str,
    status: str,
    next_action: str,
    reason: str,
    pending: list[object],
    details: dict[str, Any] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    created_at = as_of or contracts.utc_now()
    body = {
        "schema_version": contracts.SCHEMA_VERSION,
        "kind": contracts.GUIDANCE_KIND,
        "created_at": created_at,
        "as_of": as_of or created_at,
        "status": status,
        "scope": scope,
        "next_action": next_action,
        "reason": reason,
        "pending_obligations": pending,
        "authority": "advisory_only",
        "details": details or {},
    }
    return {**body, "guidance_snapshot_id": contracts.content_digest(body)}


def compact_guidance(value: dict[str, Any]) -> str:
    details = value.get("details", {})
    lines = [
        f"status: {value['status']}",
        f"snapshot: {value['guidance_snapshot_id']}",
        f"scope: {value['scope']}",
        f"next_action: {value['next_action']}",
        f"reason: {value['reason']}",
        f"pending_obligations: {len(value['pending_obligations'])}",
        f"check_required: {details.get('check_required', 'unknown')}",
        f"check_admissible_now: {details.get('check_admissible_now', 'unknown')}",
        f"historical_verification: {details.get('historical_verification', 'unknown')}",
        f"live_readiness: {details.get('live_readiness', 'unknown')}",
        "authority: advisory_only",
    ]
    return "\n".join(lines) + "\n"
