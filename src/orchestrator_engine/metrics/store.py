"""Content-addressed metrics storage with atomic generation selection."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from orchestrator_engine import core, platform_runtime

from . import contracts

REGISTRY_KIND = "ORCHESTRATOR_METRICS_REGISTRY"
SEGMENT_KIND = "ORCHESTRATOR_METRICS_SEGMENT"
BATCH_KIND = "ORCHESTRATOR_METRICS_OBSERVATION_BATCH"
SELECTOR_KIND = "ORCHESTRATOR_METRICS_CURRENT"
COLLECTOR_CURSOR_KIND = "ORCHESTRATOR_METRICS_COLLECTOR_CURSOR"
BATCH_TARGET_BYTES = 512 * 1024


class MetricsStoreError(core.OrchestratorError):
    """The metrics store is missing, corrupt or cannot be updated safely."""


class MetricsStore:
    def __init__(
        self,
        project_root: Path,
        *,
        state_dir: str = core.DEFAULT_STATE_DIR,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.root = core.state_root(self.project_root, state_dir=state_dir) / "metrics"
        self.registry_path = self.root / "registry.json"
        self.current_path = self.root / "current.json"
        self.lock_path = self.root / "writer.lock"

    def initialize(self) -> dict[str, Any]:
        self._make_directories()
        with platform_runtime.exclusive_file_lock(self.lock_path, timeout_seconds=5):
            if self.registry_path.exists():
                generation = self.current_generation(verify_objects=False)
                return {
                    "status": "existing",
                    "project_uuid": self.registry()["project_uuid"],
                    "generation": generation["generation_digest"],
                }
            registry = {
                "schema_version": contracts.SCHEMA_VERSION,
                "kind": REGISTRY_KIND,
                "project_uuid": str(uuid.uuid4()),
                "created_at": contracts.utc_now(),
                "checkout_aliases": [],
                "account_aliases": [],
                "sources": [],
            }
            core.atomic_json(self.registry_path, registry)
            manifest = self._commit_generation_unlocked(
                parent=None,
                parent_sequence=-1,
                registry=registry,
                new_observations=[],
                previous_segments=[],
            )
            return {
                "status": "initialized",
                "project_uuid": registry["project_uuid"],
                "generation": manifest["generation_digest"],
            }

    def registry(self, generation: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.registry_path.is_file():
            raise MetricsStoreError("metrics store is not initialized")
        if generation is None and self.current_path.is_file():
            generation = self.current_generation(verify_objects=False)
        value = (
            self._load_hashed(
                self._object_path(generation["registry_digest"]),
                generation["registry_digest"],
            )
            if generation is not None
            else core.load_object(self.registry_path)
        )
        if value.get("schema_version") != contracts.SCHEMA_VERSION:
            raise MetricsStoreError("unsupported metrics registry schema_version")
        if value.get("kind") != REGISTRY_KIND:
            raise MetricsStoreError("invalid metrics registry kind")
        if not isinstance(value.get("sources"), list):
            raise MetricsStoreError("metrics registry sources must be a list")
        if not isinstance(value.get("checkout_aliases", []), list):
            raise MetricsStoreError("metrics checkout aliases must be a list")
        if not isinstance(value.get("account_aliases", []), list):
            raise MetricsStoreError("metrics account aliases must be a list")
        return value

    def sources(self) -> list[dict[str, Any]]:
        return [contracts.normalize_source(item) for item in self.registry()["sources"]]

    def register_source(
        self,
        *,
        name: str,
        source_type: str,
        capabilities: list[str] | None = None,
        capability_inventory: list[dict[str, Any]] | None = None,
        source_id: str | None = None,
        scope: str = "project",
        enabled: bool = True,
        adapter_version: str = "unspecified",
        authority: str = "source_asserted",
        identity_mapping: str = "explicit_native_identity",
        observation_semantics: str = "mixed",
    ) -> dict[str, Any]:
        self._require_initialized()
        source = contracts.normalize_source(
            {
                "schema_version": contracts.SCHEMA_VERSION,
                "kind": contracts.SOURCE_KIND,
                "source_id": source_id or str(uuid.uuid4()),
                "name": name,
                "source_type": source_type,
                "adapter_version": adapter_version,
                "authority": authority,
                "identity_mapping": identity_mapping,
                "observation_semantics": observation_semantics,
                "scope": scope,
                "enabled": enabled,
                "capabilities": capabilities or [],
                "capability_inventory": capability_inventory,
                "created_at": contracts.utc_now(),
            }
        )
        with platform_runtime.exclusive_file_lock(self.lock_path, timeout_seconds=5):
            registry = self.registry()
            for existing in registry["sources"]:
                if existing["source_id"] == source["source_id"]:
                    normalized_existing = contracts.normalize_source(existing)
                    comparable = {
                        key: value
                        for key, value in source.items()
                        if key != "created_at"
                    }
                    if {
                        key: value
                        for key, value in normalized_existing.items()
                        if key != "created_at"
                    } == comparable:
                        return {"status": "existing", "source": normalized_existing}
                    raise MetricsStoreError("source_id is already registered")
                if existing["name"] == source["name"]:
                    raise MetricsStoreError("source name is already registered")
            registry = {**registry, "sources": [*registry["sources"], source]}
            current = self.current_generation(verify_objects=False)
            manifest = self._commit_generation_unlocked(
                parent=current["generation_digest"],
                parent_sequence=current["generation_sequence"],
                registry=registry,
                new_observations=[],
                previous_segments=current["segment_digests"],
            )
            core.atomic_json(self.registry_path, registry)
        return {
            "status": "registered",
            "source": source,
            "generation": manifest["generation_digest"],
        }

    def set_source_enabled(self, source_ref: str, *, enabled: bool) -> dict[str, Any]:
        self._require_initialized()
        with platform_runtime.exclusive_file_lock(self.lock_path, timeout_seconds=5):
            registry = self.registry()
            matches = [
                item
                for item in registry["sources"]
                if source_ref in {item["source_id"], item["name"]}
            ]
            if not matches:
                raise MetricsStoreError(
                    f"metrics source is not registered: {source_ref}"
                )
            source = contracts.normalize_source(matches[0])
            if source["enabled"] is enabled:
                return {"status": "unchanged", "source": source}
            updated = {**source, "enabled": enabled}
            registry = {
                **registry,
                "sources": [
                    updated if item["source_id"] == source["source_id"] else item
                    for item in registry["sources"]
                ],
            }
            current = self.current_generation(verify_objects=False)
            manifest = self._commit_generation_unlocked(
                parent=current["generation_digest"],
                parent_sequence=current["generation_sequence"],
                registry=registry,
                new_observations=[],
                previous_segments=current["segment_digests"],
            )
            core.atomic_json(self.registry_path, registry)
        return {
            "status": "updated",
            "source": updated,
            "generation": manifest["generation_digest"],
        }

    def ingest(self, values: Iterable[dict[str, Any]]) -> dict[str, Any]:
        normalized: list[dict[str, Any]] = []
        seen_ids: dict[str, str] = {}
        repeated_in_batch = 0
        for index, value in enumerate(values):
            if index >= contracts.MAX_IMPORT_RECORDS:
                raise MetricsStoreError(
                    "one import may contain at most "
                    f"{contracts.MAX_IMPORT_RECORDS} records"
                )
            item = contracts.normalize_observation(value)
            observation_id = item["observation_id"]
            digest = contracts.content_digest(item)
            previous = seen_ids.get(observation_id)
            if previous is not None:
                if previous != digest:
                    raise MetricsStoreError(
                        "observation_id appears more than once with different "
                        f"content: {observation_id}"
                    )
                repeated_in_batch += 1
                continue
            seen_ids[observation_id] = digest
            normalized.append(item)
        self._require_initialized()
        with platform_runtime.exclusive_file_lock(self.lock_path, timeout_seconds=5):
            registry = self.registry()
            sources = {item["source_id"]: item for item in registry["sources"]}
            source_ids = set(sources)
            missing = sorted({item["source_id"] for item in normalized} - source_ids)
            if missing:
                raise MetricsStoreError(f"unregistered source_id: {missing[0]}")
            disabled = sorted(
                {
                    item["source_id"]
                    for item in normalized
                    if sources[item["source_id"]].get("enabled") is not True
                }
            )
            if disabled:
                raise MetricsStoreError(f"metrics source is disabled: {disabled[0]}")
            current = self.current_generation(verify_objects=False)
            existing = self._observation_index(current)
            additions = []
            for item in normalized:
                previous_digest = existing.get(item["observation_id"])
                item_digest = contracts.content_digest(item)
                if previous_digest is None:
                    additions.append(item)
                elif previous_digest != item_digest:
                    raise MetricsStoreError(
                        "observation_id already exists with different content: "
                        f"{item['observation_id']}"
                    )
            if not additions:
                return {
                    "status": "unchanged",
                    "imported": 0,
                    "duplicates": len(normalized) + repeated_in_batch,
                    "generation": current["generation_digest"],
                }
            manifest = self._commit_generation_unlocked(
                parent=current["generation_digest"],
                parent_sequence=current["generation_sequence"],
                registry=registry,
                new_observations=additions,
                previous_segments=current["segment_digests"],
            )
        return {
            "status": "committed",
            "imported": len(additions),
            "duplicates": len(normalized) - len(additions) + repeated_in_batch,
            "generation": manifest["generation_digest"],
        }

    def collector_cursor(self, source_id: str) -> str | None:
        self._require_initialized()
        path = self.root / "collectors" / f"{source_id}.json"
        if not path.is_file():
            return None
        value = core.load_object(path)
        if value.get("schema_version") != contracts.SCHEMA_VERSION:
            raise MetricsStoreError(
                "unsupported metrics collector cursor schema_version"
            )
        if value.get("kind") != COLLECTOR_CURSOR_KIND:
            raise MetricsStoreError("invalid metrics collector cursor kind")
        if value.get("source_id") != source_id:
            raise MetricsStoreError("metrics collector cursor source differs")
        cursor = value.get("next_cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise MetricsStoreError("invalid metrics collector cursor")
        return cursor

    def set_collector_cursor(self, source_id: str, cursor: str | None) -> None:
        self._require_initialized()
        core.atomic_json(
            self.root / "collectors" / f"{source_id}.json",
            {
                "schema_version": contracts.SCHEMA_VERSION,
                "kind": COLLECTOR_CURSOR_KIND,
                "source_id": source_id,
                "next_cursor": cursor,
                "updated_at": contracts.utc_now(),
            },
        )

    def current_generation(self, *, verify_objects: bool = True) -> dict[str, Any]:
        selector = core.load_object(self.current_path)
        if selector.get("schema_version") != contracts.SCHEMA_VERSION:
            raise MetricsStoreError("unsupported metrics selector schema_version")
        if selector.get("kind") != SELECTOR_KIND:
            raise MetricsStoreError("invalid metrics generation selector")
        digest = selector.get("generation_digest")
        if not isinstance(digest, str):
            raise MetricsStoreError("generation selector has no digest")
        return self.generation(digest, verify_objects=verify_objects)

    def generation(self, digest: str, *, verify_objects: bool = True) -> dict[str, Any]:
        manifest = self._load_hashed(
            self.root / "generations" / f"{digest}.json", digest
        )
        if manifest.get("schema_version") != contracts.SCHEMA_VERSION:
            raise MetricsStoreError("unsupported metrics generation schema_version")
        if manifest.get("kind") != contracts.GENERATION_KIND:
            raise MetricsStoreError("invalid metrics generation kind")
        if manifest.get("generation_digest") != digest:
            raise MetricsStoreError(
                "generation self-reference does not match its digest"
            )
        sequence = manifest.get("generation_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise MetricsStoreError("invalid metrics generation sequence")
        parent = manifest.get("parent_generation")
        if parent is not None and (not isinstance(parent, str) or len(parent) != 64):
            raise MetricsStoreError("invalid parent generation digest")
        segments = manifest.get("segment_digests")
        if not isinstance(segments, list) or not all(
            isinstance(item, str) for item in segments
        ):
            raise MetricsStoreError("invalid generation segment list")
        if len(segments) != len(set(segments)):
            raise MetricsStoreError("duplicate metrics generation segment")
        self._load_hashed(
            self._object_path(manifest.get("registry_digest")),
            manifest.get("registry_digest"),
        )
        generation_ids: set[str] = set()
        for segment_digest in segments:
            segment = self._load_hashed(
                self.root / "segments" / f"{segment_digest}.json",
                segment_digest,
            )
            if segment.get("schema_version") != contracts.SCHEMA_VERSION:
                raise MetricsStoreError("unsupported metrics segment schema_version")
            if segment.get("kind") != SEGMENT_KIND:
                raise MetricsStoreError("invalid metrics segment kind")
            object_digests = segment.get("object_digests")
            if not isinstance(object_digests, list) or not all(
                isinstance(item, str) for item in object_digests
            ):
                raise MetricsStoreError("invalid metrics segment object list")
            actual_digests: dict[str, str] = {}
            for object_digest in object_digests:
                object_path = self._object_path(object_digest)
                if verify_objects:
                    stored = self._load_hashed(object_path, object_digest)
                    candidates = (
                        stored.get("observations", [])
                        if stored.get("kind") == BATCH_KIND
                        else [stored]
                    )
                    if not isinstance(candidates, list):
                        raise MetricsStoreError("invalid metrics observation batch")
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            raise MetricsStoreError(
                                "invalid metrics observation object"
                            )
                        item = contracts.normalize_observation(candidate)
                        observation_id = item["observation_id"]
                        if observation_id in actual_digests:
                            raise MetricsStoreError(
                                "duplicate observation in metrics segment"
                            )
                        if observation_id in generation_ids:
                            raise MetricsStoreError(
                                "duplicate observation in metrics generation"
                            )
                        actual_digests[observation_id] = contracts.content_digest(item)
                        generation_ids.add(observation_id)
                elif not object_path.is_file():
                    raise MetricsStoreError(
                        f"metrics generation object is missing: {object_path}"
                    )
            if verify_objects:
                declared_digests = segment.get("observation_digests")
                if declared_digests != actual_digests:
                    raise MetricsStoreError(
                        "metrics segment observation digest index differs"
                    )
                if segment.get("observation_ids") != list(actual_digests):
                    raise MetricsStoreError(
                        "metrics segment observation identity index differs"
                    )
        return manifest

    def observations(
        self, generation: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        generation = generation or self.current_generation()
        values: list[dict[str, Any]] = []
        ids: set[str] = set()
        for segment_digest in generation["segment_digests"]:
            segment = self._load_hashed(
                self.root / "segments" / f"{segment_digest}.json",
                segment_digest,
            )
            segment_digests: dict[str, str] = {}
            for digest in segment["object_digests"]:
                stored = self._load_hashed(self._object_path(digest), digest)
                candidates = (
                    stored.get("observations", [])
                    if stored.get("kind") == BATCH_KIND
                    else [stored]
                )
                for candidate in candidates:
                    item = contracts.normalize_observation(candidate)
                    observation_id = item["observation_id"]
                    if observation_id in segment_digests:
                        raise MetricsStoreError(
                            "duplicate observation in metrics segment"
                        )
                    if observation_id in ids:
                        raise MetricsStoreError(
                            "duplicate observation in metrics generation"
                        )
                    segment_digests[observation_id] = contracts.content_digest(item)
                    values.append(item)
                    ids.add(observation_id)
            if segment.get("observation_digests") != segment_digests:
                raise MetricsStoreError(
                    "metrics segment observation digest index differs"
                )
            if segment.get("observation_ids") != list(segment_digests):
                raise MetricsStoreError(
                    "metrics segment observation identity index differs"
                )
        return values

    def doctor(self) -> dict[str, Any]:
        try:
            generation = self.current_generation()
            observations = self.observations(generation)
            sources = self.sources()
        except (OSError, RuntimeError, ValueError) as error:
            return {
                "schema_version": 1,
                "kind": "ORCHESTRATOR_METRICS_DOCTOR",
                "status": "error",
                "reason": str(error),
            }
        return {
            "schema_version": 1,
            "kind": "ORCHESTRATOR_METRICS_DOCTOR",
            "status": "ok",
            "generation": generation["generation_digest"],
            "source_count": len(sources),
            "enabled_source_count": sum(1 for item in sources if item["enabled"]),
            "observation_count": len(observations),
        }

    def recover(self) -> dict[str, Any]:
        if not self.registry_path.is_file():
            raise MetricsStoreError(
                "metrics store is not initialized; run metrics init"
            )
        self._make_directories()
        with platform_runtime.exclusive_file_lock(self.lock_path, timeout_seconds=5):
            candidates: list[dict[str, Any]] = []
            for path in sorted((self.root / "generations").glob("*.json")):
                try:
                    candidates.append(self.generation(path.stem))
                except (OSError, RuntimeError, ValueError):
                    continue
            if not candidates:
                raise MetricsStoreError("no valid metrics generation is recoverable")
            selected = max(
                candidates,
                key=lambda item: (
                    item.get("generation_sequence", -1),
                    item["created_at"],
                ),
            )
            core.atomic_json(
                self.current_path,
                {
                    "schema_version": 1,
                    "kind": SELECTOR_KIND,
                    "generation_digest": selected["generation_digest"],
                    "updated_at": contracts.utc_now(),
                },
            )
            registry = self.registry(selected)
            core.atomic_json(self.registry_path, registry)
        return {"status": "recovered", "generation": selected["generation_digest"]}

    def _commit_generation_unlocked(
        self,
        *,
        parent: str | None,
        parent_sequence: int,
        registry: dict[str, Any],
        new_observations: list[dict[str, Any]],
        previous_segments: list[str],
    ) -> dict[str, Any]:
        registry_digest = self._put_object(registry)
        segments = list(previous_segments)
        if new_observations:
            object_digests = [
                self._put_object(
                    {
                        "schema_version": 1,
                        "kind": BATCH_KIND,
                        "observations": batch,
                    }
                )
                for batch in _observation_batches(new_observations)
            ]
            segment_without_digest = {
                "schema_version": 1,
                "kind": SEGMENT_KIND,
                "created_at": contracts.utc_now(),
                "object_digests": object_digests,
                "observation_ids": [
                    item["observation_id"] for item in new_observations
                ],
                "observation_digests": {
                    item["observation_id"]: contracts.content_digest(item)
                    for item in new_observations
                },
            }
            if len(contracts.canonical_bytes(segment_without_digest)) > (
                contracts.MAX_MANIFEST_BYTES
            ):
                raise MetricsStoreError("metrics segment exceeds the 1 MiB limit")
            segment_digest = contracts.content_digest(segment_without_digest)
            self._write_hashed(
                self.root / "segments" / f"{segment_digest}.json",
                segment_without_digest,
            )
            segments.append(segment_digest)
        body = {
            "schema_version": contracts.SCHEMA_VERSION,
            "kind": contracts.GENERATION_KIND,
            "created_at": contracts.utc_now(),
            "generation_sequence": parent_sequence + 1,
            "parent_generation": parent,
            "registry_digest": registry_digest,
            "segment_digests": segments,
        }
        digest = contracts.content_digest(body)
        manifest = {**body, "generation_digest": digest}
        # The digest intentionally excludes its own field.
        self._write_hashed(
            self.root / "generations" / f"{digest}.json",
            manifest,
            digest_value=body,
        )
        core.atomic_json(
            self.current_path,
            {
                "schema_version": 1,
                "kind": SELECTOR_KIND,
                "generation_digest": digest,
                "updated_at": contracts.utc_now(),
            },
        )
        return manifest

    def _put_object(self, value: dict[str, Any]) -> str:
        digest = contracts.content_digest(value)
        self._write_hashed(self._object_path(digest), value)
        return digest

    def _write_hashed(
        self,
        path: Path,
        value: dict[str, Any],
        *,
        digest_value: dict[str, Any] | None = None,
    ) -> None:
        digest = path.stem
        if contracts.content_digest(digest_value or value) != digest:
            raise MetricsStoreError("content digest does not match target path")
        if not core.claim_json(path, value):
            existing = core.load_object(path)
            if existing != value:
                raise MetricsStoreError(f"immutable metrics object differs: {path}")

    def _load_hashed(self, path: Path, digest: object) -> dict[str, Any]:
        if not isinstance(digest, str) or len(digest) != 64:
            raise MetricsStoreError("invalid content digest")
        value = core.load_object(path)
        digest_value = value
        if value.get("kind") == contracts.GENERATION_KIND:
            digest_value = {
                key: item for key, item in value.items() if key != "generation_digest"
            }
        if contracts.content_digest(digest_value) != digest:
            raise MetricsStoreError(f"metrics content hash mismatch: {path}")
        return value

    def _object_path(self, digest: object) -> Path:
        if not isinstance(digest, str) or len(digest) != 64:
            raise MetricsStoreError("invalid object digest")
        return self.root / "objects" / digest[:2] / f"{digest}.json"

    def _make_directories(self) -> None:
        for name in (
            "objects",
            "segments",
            "generations",
            "derived",
            "staging",
            "collectors",
        ):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _require_initialized(self) -> None:
        if not self.registry_path.is_file() or not self.current_path.is_file():
            raise MetricsStoreError(
                "metrics store is not initialized; run metrics init"
            )

    def _observation_index(self, generation: dict[str, Any]) -> dict[str, str]:
        values: dict[str, str] = {}
        for segment_digest in generation["segment_digests"]:
            segment = self._load_hashed(
                self.root / "segments" / f"{segment_digest}.json",
                segment_digest,
            )
            declared = segment.get("observation_digests")
            if isinstance(declared, dict) and all(
                isinstance(key, str) and isinstance(item, str)
                for key, item in declared.items()
            ):
                values.update(declared)
                continue
            for digest in segment["object_digests"]:
                stored = self._load_hashed(self._object_path(digest), digest)
                candidates = (
                    stored.get("observations", [])
                    if stored.get("kind") == BATCH_KIND
                    else [stored]
                )
                for item in candidates:
                    if isinstance(item, dict) and isinstance(
                        item.get("observation_id"), str
                    ):
                        values[item["observation_id"]] = contracts.content_digest(item)
        return values


def load_json_records(path: Path) -> list[dict[str, Any]]:
    maximum_bytes = contracts.MAX_IMPORT_RECORDS * contracts.MAX_OBSERVATION_BYTES
    if path.stat().st_size > maximum_bytes:
        raise MetricsStoreError(
            f"metrics import exceeds the {maximum_bytes}-byte input limit"
        )
    if path.suffix == ".jsonl":
        values = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                if len(line.encode("utf-8")) > contracts.MAX_OBSERVATION_BYTES:
                    raise MetricsStoreError("metrics JSONL record exceeds 64 KiB")
                values.append(json.loads(line))
                if len(values) > contracts.MAX_IMPORT_RECORDS:
                    break
    else:
        text = path.read_text(encoding="utf-8")
        loaded = json.loads(text)
        values = loaded if isinstance(loaded, list) else [loaded]
    if not all(isinstance(item, dict) for item in values):
        raise MetricsStoreError("metrics import must contain JSON objects")
    return values


def _observation_batches(
    observations: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 96
    for observation in observations:
        observation_bytes = len(contracts.canonical_bytes(observation)) + 1
        if current and current_bytes + observation_bytes > BATCH_TARGET_BYTES:
            batches.append(current)
            current = [observation]
            current_bytes = 96 + observation_bytes
        else:
            current.append(observation)
            current_bytes += observation_bytes
    if current:
        batches.append(current)
    return batches
