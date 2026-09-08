"""CLI implementation for the lazily loaded metrics subsystem."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

from . import contracts
from .adapters import collect_orchestrator_engine
from .advisor import advise, compact_guidance, outage_guidance
from .catalog import METRIC_DEFINITIONS, METRICS_BY_ID
from .progress import build_progress_report, markdown_progress
from .reporting import build_report, compare_reports, markdown_report
from .store import MetricsStore, MetricsStoreError, load_json_records


def run(args: Namespace, root: Path, *, state_dir: str) -> object:
    store = MetricsStore(root, state_dir=state_dir)
    command = args.metrics_command
    if command == "init":
        return store.initialize()
    if command == "sources":
        if args.metrics_sources_command == "list":
            return {"status": "ok", "sources": store.sources()}
        if args.metrics_sources_command == "set-enabled":
            return store.set_source_enabled(args.source, enabled=args.enabled)
        return store.register_source(
            name=args.name,
            source_type=args.type,
            capabilities=args.capability,
            capability_inventory=(
                _json_array(args.capability_inventory, field="--capability-inventory")
                if args.capability_inventory
                else None
            ),
            source_id=args.source_id,
            scope=args.scope,
            enabled=not args.disabled,
            adapter_version=args.adapter_version,
            authority=args.authority,
            identity_mapping=args.identity_mapping,
            observation_semantics=args.observation_semantics,
        )
    if command == "record":
        data = _json_object(args.data, field="--data")
        source_id = _source_id(store, args.source)
        observation = contracts.make_observation(
            source_id=source_id,
            record_type=args.record_type,
            data=data,
            observed_at=args.observed_at,
            effective_at=args.effective_at,
            known_at=args.known_at,
            observation_id=args.observation_id,
            scope=_json_object(args.scope, field="--scope"),
        )
        return store.ingest([observation])
    if command == "ingest":
        return store.ingest(load_json_records(args.input))
    if command == "collect":
        _bounded_count(args.maximum)
        source_id = _source_id(store, args.source)
        cursor = store.collector_cursor(source_id)
        result = collect_orchestrator_engine(
            root,
            source_id=source_id,
            state_dir=state_dir,
            maximum=args.maximum,
            cursor=cursor,
        )
        if args.dry_run:
            return result
        committed = store.ingest(result["records"])
        store.set_collector_cursor(source_id, result["next_cursor"])
        return {**result, "records": [], "commit": committed}
    if command == "report":
        report = build_report(
            store,
            generation_digest=args.generation,
            evaluation_time=args.evaluation_time,
            package_id=args.package_id,
            operation_id=args.operation_id,
        )
        output: object = (
            markdown_report(report) if args.format == "markdown" else report
        )
        return _write_or_return(output, args.output)
    if command == "progress":
        report = build_progress_report(
            store,
            generation_digest=args.generation,
            evaluation_time=args.evaluation_time,
            baseline_revision=args.baseline_revision,
            current_revision=args.current_revision,
            module_id=args.module_id,
            minimum_samples=args.minimum_samples,
        )
        output = markdown_progress(report) if args.format == "markdown" else report
        return _write_or_return(output, args.output)
    if command == "explain":
        if args.metric_id:
            return METRICS_BY_ID[args.metric_id]
        return {"schema_version": 1, "metrics": list(METRIC_DEFINITIONS)}
    if command == "compare":
        evaluation_time = contracts.utc_now()
        baseline = build_report(
            store,
            generation_digest=args.baseline,
            evaluation_time=evaluation_time,
        )
        candidate = build_report(
            store,
            generation_digest=args.candidate,
            evaluation_time=evaluation_time,
        )
        return compare_reports(baseline, candidate)
    if command == "advise":
        try:
            generation = store.current_generation()
            registry = store.registry(generation)
            enabled_sources = {
                item["source_id"]
                for item in registry["sources"]
                if item["enabled"] is True
            }
            project_owner_sources = {
                item["source_id"]
                for item in registry["sources"]
                if item["enabled"] is True
                and item.get("authority") == "project_owner"
            }
            source_semantics = {
                item["source_id"]: item["observation_semantics"]
                for item in registry["sources"]
                if item["enabled"] is True
            }
            canonical_sources = {
                item["source_id"]
                for item in registry["sources"]
                if item["enabled"] is True
                and item.get("identity_mapping") == "canonical_identity_authorized"
            }
            guidance = advise(
                [
                    item
                    for item in store.observations(generation)
                    if item["source_id"] in enabled_sources
                ],
                scope=args.scope,
                package_id=args.package_id,
                operation_id=args.operation_id,
                generation_digest=generation["generation_digest"],
                project_owner_source_ids=project_owner_sources,
                canonical_source_ids=canonical_sources,
                source_observation_semantics=source_semantics,
                evaluation_time=args.evaluation_time,
            )
        except (OSError, RuntimeError, ValueError) as error:
            guidance = outage_guidance(scope=args.scope, reason=str(error)[:240])
        return compact_guidance(guidance) if args.format == "text" else guidance
    if command == "doctor":
        return store.doctor()
    if command == "migrate":
        # Version 1 has no transform. Explicit recovery only repairs the selector.
        return (
            store.recover()
            if args.recover
            else {
                "status": "current",
                "schema_version": contracts.SCHEMA_VERSION,
            }
        )
    if command == "export":
        _bounded_count(args.maximum)
        generation = (
            store.generation(args.generation)
            if args.generation
            else store.current_generation()
        )
        observations = store.observations(generation)
        selected = observations[: args.maximum]
        payload = {
            "schema_version": 1,
            "kind": "ORCHESTRATOR_METRICS_EXPORT",
            "generation_digest": generation["generation_digest"],
            "observations": selected,
            "observation_count": len(selected),
            "total_observation_count": len(observations),
            "truncated": len(selected) < len(observations),
        }
        return _write_or_return(payload, args.output)
    raise MetricsStoreError(f"unsupported metrics command: {command}")


def _source_id(store: MetricsStore, value: str) -> str:
    for source in store.sources():
        if value in {source["source_id"], source["name"]}:
            return source["source_id"]
    raise MetricsStoreError(f"metrics source is not registered: {value}")


def _json_object(value: str, *, field: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as error:
        raise MetricsStoreError(f"{field} must be valid JSON") from error
    if not isinstance(loaded, dict):
        raise MetricsStoreError(f"{field} must be a JSON object")
    return loaded


def _json_array(path: Path, *, field: str) -> list[dict[str, Any]]:
    if path.stat().st_size > contracts.MAX_MANIFEST_BYTES:
        raise MetricsStoreError(f"{field} exceeds the 1 MiB input limit")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MetricsStoreError(f"{field} must be a readable JSON array") from error
    if not isinstance(loaded, list) or not all(
        isinstance(item, dict) for item in loaded
    ):
        raise MetricsStoreError(f"{field} must be a JSON array of objects")
    return loaded


def _write_or_return(value: object, path: Path | None) -> object:
    if path is None:
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {"status": "written", "output": str(path)}


def _bounded_count(value: int) -> int:
    if not 1 <= value <= contracts.MAX_IMPORT_RECORDS:
        raise MetricsStoreError(
            f"maximum must be between 1 and {contracts.MAX_IMPORT_RECORDS}"
        )
    return value
