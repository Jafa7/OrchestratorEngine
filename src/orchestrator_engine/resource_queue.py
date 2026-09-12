"""Transactional, provider-neutral admission for registered local resources.

The ledger records ownership; neither a heartbeat nor an expired reservation
releases it. Process execution and resource-specific probes live outside it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from . import binding, core, worker_lease

TERMINAL = {"passed", "failed", "cancelled", "invalidated", "skipped"}
OWNING = {"granted", "launching", "running", "cleanup", "recovery_required"}
PRIVATE_STAGE_FIELDS = {"launch_token", "maintenance_token", "commands"}


class ResourceError(core.OrchestratorError):
    """Invalid resource contract or unavailable authority."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 160:
        raise ResourceError("expected a nonempty identifier of at most 160 characters")
    return value


def positive(value):
    if type(value) is not int or value < 1:
        raise ResourceError("capacity and units must be positive integers")
    return value


def validate_subscriber(subscriber):
    if not isinstance(subscriber, dict) or set(subscriber) - {
        "id",
        "wake_target",
        "wake",
        "wake_policy",
        "check_id",
    }:
        raise ResourceError("unsupported subscription fields")
    identifier(subscriber.get("id"))
    if "wake" in subscriber and type(subscriber["wake"]) is not bool:
        raise ResourceError("subscriber wake must be boolean")
    if subscriber.get("wake_policy", "never") not in ("never", "always", "on-failure"):
        raise ResourceError("unsupported subscriber wake policy")
    if "check_id" in subscriber:
        core.validate_event_id(subscriber["check_id"])
    target = subscriber.get("wake_target")
    if target is not None:
        if not isinstance(target, dict):
            raise ResourceError("wake target must be an object")
        binding.validate_wake_target(target)


def same_subscriber(first, second):
    """Compare immutable routing and delivery semantics, not capture time."""

    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    fields = {"id", "wake", "wake_policy", "check_id"}
    if any(first.get(field) != second.get(field) for field in fields):
        return False
    return binding.same_wake_destination(
        first.get("wake_target"), second.get("wake_target")
    )


def normalize_registry(raw):
    if not isinstance(raw, dict):
        raise ResourceError("resources must be an object")
    result = {}
    physical = set()
    for name, entry in raw.items():
        identifier(name)
        if not isinstance(entry, dict):
            raise ResourceError("resource definition must be an object")
        entry = dict(entry)
        kind = entry.get("kind", "resource")
        if kind == "resource":
            key = identifier(entry.get("physical_id", name))
            if key in physical:
                raise ResourceError("duplicate physical resource; use an alias")
            physical.add(key)
            entry.update(
                kind=kind,
                physical_id=key,
                capacity=positive(entry.get("capacity", 1)),
                incarnation=identifier(entry.get("incarnation", "1")),
            )
            if entry.get("release") not in {"process", "probe"}:
                raise ResourceError("each leaf must declare process or probe release")
            if entry["release"] == "probe" and not entry.get("probe"):
                raise ResourceError("probe release requires a registered probe command")
        elif kind in {"alias", "pool", "bundle"}:
            members = entry.get("members")
            if (
                not isinstance(members, list)
                or not members
                or len(set(members)) != len(members)
            ):
                raise ResourceError("selectors need distinct nonempty members")
            if kind == "alias" and len(members) != 1:
                raise ResourceError("alias must identify one target")
        else:
            raise ResourceError("unsupported resource kind")
        entry["kind"] = kind
        result[name] = entry
    for name in result:
        if next(alternatives(result, name), None) is None:
            raise ResourceError("bundle has no assignment without overlapping members")
    return result


def selector_leaves(registry, name, trail=()):
    if name in trail or name not in registry:
        raise ResourceError("unknown or cyclic resource selector")
    entry = registry[name]
    if entry["kind"] == "resource":
        return {name}
    leaves = set()
    for member in entry["members"]:
        leaves.update(selector_leaves(registry, member, (*trail, name)))
    return leaves


def selector_available(registry, name, *, excluded=(), trail=()):
    """Return whether a selector has a structurally possible choice."""

    if name in trail or name not in registry:
        raise ResourceError("unknown or cyclic resource selector")
    entry = registry[name]
    if entry["kind"] == "resource":
        return name not in excluded
    available = [
        selector_available(
            registry,
            member,
            excluded=excluded,
            trail=(*trail, name),
        )
        for member in entry["members"]
    ]
    if entry["kind"] in {"alias", "pool"}:
        return any(available)
    return all(available)


def selector_claim_available(
    registry,
    name,
    *,
    claim,
    others,
    excluded=(),
    trail=(),
):
    """Return whether every mandatory branch can satisfy one claim."""

    if name in trail or name not in registry:
        raise ResourceError("unknown or cyclic resource selector")
    entry = registry[name]
    if entry["kind"] == "resource":
        return name not in excluded and compatible(
            registry, {name: dict(claim)}, others
        )
    checks = (
        selector_claim_available(
            registry,
            member,
            claim=claim,
            others=others,
            excluded=excluded,
            trail=(*trail, name),
        )
        for member in entry["members"]
    )
    if entry["kind"] in {"alias", "pool"}:
        return any(checks)
    return all(checks)


def alternatives(registry, name, trail=(), *, priority=None, excluded=()):
    """Yield selector alternatives lazily, including nested bundles."""

    if name in trail or name not in registry:
        raise ResourceError("unknown or cyclic resource selector")
    entry = registry[name]
    if entry["kind"] == "resource":
        if name not in excluded:
            yield (name,)
        return
    if not selector_available(registry, name, excluded=excluded, trail=trail):
        return
    priority = priority or {}
    members = sorted(
        entry["members"],
        key=lambda member: (
            min(
                (priority.get(leaf, 0) for leaf in selector_leaves(registry, member)),
                default=0,
            ),
            member,
        ),
    )
    if entry["kind"] in {"alias", "pool"}:
        seen = set()
        for member in members:
            for choice in alternatives(
                registry,
                member,
                (*trail, name),
                priority=priority,
                excluded=excluded,
            ):
                if choice not in seen:
                    seen.add(choice)
                    yield choice
        return

    seen = set()

    def combine(index, selected):
        if index == len(members):
            choice = tuple(sorted(selected))
            if choice not in seen:
                seen.add(choice)
                yield choice
            return
        for option in alternatives(
            registry,
            members[index],
            (*trail, name),
            priority=priority,
            excluded=excluded,
        ):
            if selected.intersection(option):
                continue
            yield from combine(index + 1, selected | set(option))

    yield from combine(0, set())


def demands(registry, needs):
    if not isinstance(needs, list):
        raise ResourceError("needs must be a list")
    result = []
    for need in needs:
        if not isinstance(need, dict) or "resource" not in need:
            raise ResourceError("need requires a resource identifier")
        name = identifier(need["resource"])
        mode = need.get("mode", "exclusive")
        if mode not in {"exclusive", "shared"}:
            raise ResourceError("unsupported resource mode")
        units = positive(need.get("units", 1))
        group = need.get("compatibility")
        if mode == "shared":
            identifier(group)
        if mode == "exclusive" and units != 1:
            raise ResourceError("exclusive access consumes the entire leaf")
        if name not in registry:
            raise ResourceError("unknown or cyclic resource selector")
        result.append((name, mode, units, group))
    return result


def assignments(registry, needs, *, others=(), excluded=(), priority=None, required=()):
    """Lazily backtrack, pruning incompatible partial assignments immediately."""
    specifications = demands(registry, needs)
    priority = priority or {}
    excluded, required = set(excluded), set(required)
    remaining = [set() for _ in range(len(specifications) + 1)]
    for index in reversed(range(len(specifications))):
        remaining[index] = remaining[index + 1] | selector_leaves(
            registry, specifications[index][0]
        )

    # Explicit DFS frames preserve lazy search without a Python recursion ceiling.
    stack = [(0, {}, None)]
    while stack:
        index, allocation, iterator = stack[-1]
        if required and not required & (set(allocation) | remaining[index]):
            stack.pop()
            continue
        if index == len(specifications):
            stack.pop()
            yield allocation
            continue
        if iterator is None:
            _, mode, units, group = specifications[index]
            claim = {
                "mode": mode,
                "units": units,
                "compatibility": group,
            }
            if not selector_claim_available(
                registry,
                specifications[index][0],
                claim=claim,
                others=others,
                excluded=excluded,
            ):
                stack.pop()
                continue
            unavailable = {
                leaf
                for leaf in selector_leaves(registry, specifications[index][0])
                if not compatible(
                    registry,
                    {leaf: claim},
                    others,
                )
            }
            iterator = alternatives(
                registry,
                specifications[index][0],
                priority=priority,
                excluded=excluded | unavailable,
            )
            stack[-1] = (index, allocation, iterator)
        leaves = next(iterator, None)
        if leaves is None:
            stack.pop()
            continue
        _, mode, units, group = specifications[index]
        candidate = dict(allocation)
        for leaf in leaves:
            claim = {"mode": mode, "units": units, "compatibility": group}
            if leaf in candidate:
                old = candidate[leaf]
                if (
                    mode != "shared"
                    or old["mode"] != mode
                    or old["compatibility"] != group
                ):
                    break
                claim["units"] += old["units"]
            if claim["units"] > registry[leaf]["capacity"]:
                break
            candidate[leaf] = claim
        else:
            if compatible(registry, candidate, others):
                stack.append((index + 1, candidate, None))


def compatible(registry, allocation, others):
    for leaf, claim in allocation.items():
        if leaf in registry and claim["units"] > registry[leaf]["capacity"]:
            return False
        occupied = [other[leaf] for other in others if leaf in other]
        if not occupied:
            continue
        if claim["mode"] == "exclusive" or any(
            item["mode"] != "shared" or item["compatibility"] != claim["compatibility"]
            for item in occupied
        ):
            return False
        if (
            claim["units"] + sum(item["units"] for item in occupied)
            > registry[leaf]["capacity"]
        ):
            return False
    return True


class Ledger:
    """One native-local SQLite authority; create is always explicit."""

    def __init__(self, directory: Path, *, create=False, clock=time.time):
        self.directory = Path(directory).resolve()
        self.clock = clock
        path = self.directory / "resources.sqlite3"
        if not create and not path.is_file():
            raise ResourceError(
                "resource ledger is missing; refusing implicit reinitialization"
            )
        if create:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        if create and os.name != "nt":
            try:
                path.chmod(0o600)
            except OSError as error:
                self.db.close()
                raise ResourceError(
                    "resource ledger permissions could not be restricted"
                ) from error
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        if create:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta
                  (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests
                  (id TEXT PRIMARY KEY, project TEXT NOT NULL, external TEXT NOT NULL,
                   body TEXT NOT NULL, UNIQUE(project, external));
                CREATE TABLE IF NOT EXISTS stages
                  (id TEXT PRIMARY KEY, request TEXT NOT NULL REFERENCES requests(id),
                   state TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS stage_state ON stages(state,seq);
                CREATE INDEX IF NOT EXISTS stage_request ON stages(request,seq);
                CREATE TABLE IF NOT EXISTS events
                  (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                   subject TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_subject ON events(subject,seq);
                CREATE TABLE IF NOT EXISTS outbox
                  (id TEXT PRIMARY KEY, request TEXT NOT NULL, body TEXT NOT NULL,
                   delivered INTEGER NOT NULL DEFAULT 0,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt REAL NOT NULL DEFAULT 0,
                   last_error TEXT);
                CREATE TABLE IF NOT EXISTS subscriber_history
                  (request TEXT NOT NULL, subscriber TEXT NOT NULL,
                   generation INTEGER NOT NULL, body TEXT NOT NULL,
                   active INTEGER NOT NULL,
                   PRIMARY KEY(request,subscriber,generation));
            """)
            for key, value in {
                "version": 1,
                "authority": str(uuid.uuid4()),
                "sequence": 0,
                "registry": {},
                "released": [],
            }.items():
                self.db.execute(
                    "INSERT OR IGNORE INTO meta VALUES (?,?)", (key, canonical(value))
                )
        if self.get("version") != 1:
            raise ResourceError("unsupported resource ledger schema")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS subscriber_history "
            "(request TEXT NOT NULL, subscriber TEXT NOT NULL, "
            "generation INTEGER NOT NULL, body TEXT NOT NULL, "
            "active INTEGER NOT NULL, "
            "PRIMARY KEY(request,subscriber,generation))"
        )

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise ResourceError(f"ledger metadata missing: {key}")
        return json.loads(row[0])

    def put(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value))
        )

    def event(self, subject, kind, **body):
        self.db.execute(
            "INSERT INTO events(at,subject,kind,body) VALUES (?,?,?,?)",
            (self.clock(), subject, kind, canonical(body)),
        )

    def sequence(self):
        value = self.get("sequence") + 1
        self.put("sequence", value)
        return value

    def stages(self, request=None, *, active=False, project=None):
        query, args, conditions = "SELECT s.body FROM stages AS s", [], []
        if project is not None:
            query += " JOIN requests AS r ON s.request=r.id"
            conditions.append("r.project=?")
            args.append(project)
        if request:
            conditions.append("s.request=?")
            args.append(request)
        elif active:
            conditions.append(
                "s.state NOT IN ('passed','failed','cancelled','invalidated','skipped')"
            )
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        return [
            json.loads(row[0])
            for row in self.db.execute(query + " ORDER BY s.seq,s.id", args)
        ]

    def stage(self, stage_id):
        row = self.db.execute(
            "SELECT body FROM stages WHERE id=?", (stage_id,)
        ).fetchone()
        if not row:
            raise ResourceError("unknown stage")
        return json.loads(row[0])

    def save(self, stage):
        row = self.db.execute(
            "SELECT body FROM stages WHERE id=?", (stage["id"],)
        ).fetchone()
        old = json.loads(row[0]) if row else None
        if old is None or (old["state"], old.get("reason")) != (
            stage["state"],
            stage.get("reason"),
        ):
            now = self.clock()
            accumulated = dict(old.get("wait_seconds", {})) if old else {}
            if old and old["state"] == "waiting":
                duration = now - old.get("transition_at", old["created_at"])
                reason = old.get("reason", "unknown")
                previous = accumulated.get(reason, 0)
                accumulated[reason] = (
                    previous + duration
                    if previous is not None and duration >= 0
                    else None
                )
            stage.update(wait_seconds=accumulated, transition_at=now)
            self.event(
                stage["id"],
                "state_changed",
                state=stage["state"],
                reason=stage.get("reason"),
            )
        self.db.execute(
            "INSERT OR REPLACE INTO stages VALUES (?,?,?,?,?)",
            (
                stage["id"],
                stage["request"],
                stage["state"],
                stage["seq"],
                canonical(stage),
            ),
        )

    def configure(self, raw):
        registry = normalize_registry(raw)
        with self.transaction():
            old = self.get("registry")
            for stage in self.stages(active=True):
                for leaf in stage.get("allocation", {}):
                    if registry.get(leaf) != old.get(leaf):
                        raise ResourceError(
                            "active resource changes require drain and release first"
                        )
                if stage["state"] == "waiting" and registry != old:
                    raise ResourceError(
                        "cancel pending requests before registry changes"
                    )
            self.put("registry", registry)
            self.event("registry", "configured", digest=digest(registry))

    def submit(self, project, external, plan, subscriber=None):
        identifier(project)
        identifier(external)
        if subscriber is not None:
            validate_subscriber(subscriber)
        definitions = plan.get("stages")
        if not isinstance(definitions, list) or not definitions:
            raise ResourceError("plan requires stages")
        names = [identifier(stage["id"]) for stage in definitions]
        if len(set(names)) != len(names):
            raise ResourceError("duplicate stage name")
        remaining = set(names)
        while remaining:
            ready = {
                s["id"]
                for s in definitions
                if s["id"] in remaining and not set(s.get("after", [])) & remaining
            }
            if not ready:
                raise ResourceError("cyclic stage dependencies")
            remaining -= ready
        with self.transaction():
            row = self.db.execute(
                "SELECT id,body FROM requests WHERE project=? AND external=?",
                (project, external),
            ).fetchone()
            if row:
                if json.loads(row[1])["plan"] != plan:
                    raise ResourceError(
                        "request ID already has different immutable inputs"
                    )
                return row[0]
            registry = self.get("registry")
            for definition in definitions:
                if set(definition.get("after", [])) - set(names):
                    raise ResourceError("unknown stage dependency")
                if (
                    next(assignments(registry, definition.get("needs", [])), None)
                    is None
                ):
                    raise ResourceError(
                        "resource requirements have no complete assignment"
                    )
            request_id = str(uuid.uuid4())
            body = {
                "id": request_id,
                "project": project,
                "external": external,
                "plan": plan,
                "subscribers": [subscriber] if subscriber else [],
                "created_at": self.clock(),
            }
            self.db.execute(
                "INSERT INTO requests VALUES (?,?,?,?)",
                (request_id, project, external, canonical(body)),
            )
            if subscriber is not None:
                self.db.execute(
                    "INSERT INTO subscriber_history VALUES (?,?,?,?,1)",
                    (request_id, subscriber["id"], 1, canonical(subscriber)),
                )
            for definition in definitions:
                stage = {
                    **definition,
                    "id": str(uuid.uuid4()),
                    "name": definition["id"],
                    "request": request_id,
                    "project": project,
                    "state": "waiting",
                    "seq": self.sequence(),
                    "reason": "dependency_wait",
                    "created_at": self.clock(),
                    "epoch": 0,
                    "allocation": {},
                }
                for key in (
                    "ready_at",
                    "ready_seq",
                    "protection",
                    "launch_token",
                    "maintenance_token",
                    "cancel_requested",
                    "supervisor_identity",
                ):
                    stage.pop(key, None)
                self.save(stage)
            self.event(request_id, "submitted", project=project, external=external)
            return request_id

    def request(self, request_id):
        row = self.db.execute(
            "SELECT body FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            raise ResourceError("unknown resource request")
        return json.loads(row[0])

    def schedule(self, unavailable=()):
        """Oldest feasible grants with release-triggered conflicting protection."""
        granted = []
        with self.transaction():
            registry = self.get("registry")
            stages = self.stages(active=True)
            released = set(self.get("released"))
            self.put("released", [])
            active = [s for s in stages if s["state"] in OWNING]
            quarantine = {
                leaf
                for s in active
                if s["state"] == "recovery_required"
                for leaf in s["allocation"]
            }
            protection = []
            ready = []
            previously_ready = {s["id"] for s in stages if "ready_seq" in s}
            for stage in stages:
                if stage["state"] != "waiting":
                    continue
                siblings = self.stages(stage["request"])
                predecessors = [
                    s for s in siblings if s["name"] in stage.get("after", [])
                ]
                reason = None
                if any(
                    s["state"] in (TERMINAL - {"passed"})
                    or (
                        s["state"] == "recovery_required"
                        and s.get("outcome") != "passed"
                    )
                    for s in predecessors
                ):
                    stage["state"] = "skipped"
                    stage["finished_at"] = self.clock()
                    reason = "dependency_failed"
                elif any(s["state"] != "passed" for s in predecessors):
                    reason = "dependency_wait"
                elif stage["project"] in unavailable:
                    reason = "host_wait"
                if reason:
                    stage.pop("protection", None)
                    stage.pop("ready_seq", None)
                    stage["reason"] = reason
                    self.save(stage)
                    continue
                if "ready_seq" not in stage:
                    stage["ready_at"] = self.clock()
                    stage["ready_seq"] = self.sequence()
                    self.event(stage["id"], "ready")
                ready.append(stage)
            for stage in sorted(ready, key=lambda s: (s["ready_seq"], s["id"])):
                occupied = [s["allocation"] for s in active]
                # Preserve scarce alternatives for less flexible pending requests.
                demand_count = {}
                for other in stages:
                    if other["id"] == stage["id"] or other["state"] != "waiting":
                        continue
                    for selector, *_ in demands(registry, other.get("needs", [])):
                        for leaf in selector_leaves(registry, selector):
                            demand_count[leaf] = demand_count.get(leaf, 0) + 1
                needs = stage.get("needs", [])
                chosen = next(
                    assignments(
                        registry,
                        needs,
                        others=occupied + protection,
                        excluded=quarantine,
                        priority=demand_count,
                    ),
                    None,
                )
                if chosen is not None:
                    stage.update(
                        state="granted",
                        allocation=chosen,
                        epoch=stage["epoch"] + 1,
                        launch_token=secrets.token_urlsafe(32),
                        maintenance_token=secrets.token_urlsafe(32),
                        granted_at=self.clock(),
                        reason=None,
                    )
                    stage["resource_incarnations"] = {
                        leaf: registry[leaf]["incarnation"] for leaf in chosen
                    }
                    stage["granted_allocation"] = chosen
                    stage.pop("protection", None)
                    self.save(stage)
                    self.event(
                        stage["id"],
                        "granted",
                        allocation=chosen,
                        epoch=stage["epoch"],
                        capacities={
                            leaf: registry[leaf]["capacity"] for leaf in chosen
                        },
                    )
                    for bypassed in ready:
                        if (
                            bypassed["state"] == "waiting"
                            and bypassed["ready_seq"] < stage["ready_seq"]
                        ):
                            bypassed["bypass_count"] = (
                                bypassed.get("bypass_count", 0) + 1
                            )
                            self.save(bypassed)
                    granted.append(stage)
                    active.append(stage)
                    continue
                previous = stage.get("protection")
                candidate = previous
                # Waiting contracts and their registry are immutable, so a stored
                # protection stays valid without enumerating every alternative.
                if candidate is not None and (
                    set(candidate) & quarantine
                    or not compatible(registry, candidate, protection)
                ):
                    candidate = None
                if candidate is None and released and stage["id"] in previously_ready:
                    candidate = next(
                        assignments(
                            registry,
                            needs,
                            others=protection,
                            excluded=quarantine,
                            priority=demand_count,
                            required=released,
                        ),
                        None,
                    )
                stage.pop("protection", None)
                if candidate is not None:
                    stage["protection"] = candidate
                    protection.append(candidate)
                    if candidate != previous:
                        self.event(stage["id"], "protected", allocation=candidate)
                stage["reason"] = (
                    "recovery_blocked"
                    if next(assignments(registry, needs, excluded=quarantine), None)
                    is None
                    else "resource_wait"
                )
                stage["blockers"] = [
                    s["id"]
                    for s in active
                    if any(
                        not compatible(
                            registry,
                            {
                                leaf: {
                                    "mode": mode,
                                    "units": units,
                                    "compatibility": group,
                                }
                            },
                            [s["allocation"]],
                        )
                        for options, mode, units, group in demands(registry, needs)
                        for leaves in options
                        for leaf in leaves
                    )
                ]
                self.save(stage)
            self._outcomes({s["request"] for s in stages})
        return granted

    def attach(self, stage_id, epoch, identity):
        with self.transaction():
            stage = self.stage(stage_id)
            if stage["state"] != "granted" or stage["epoch"] != epoch:
                raise ResourceError("revoked launch intent")
            stage.update(state="launching", supervisor_identity=identity)
            self.save(stage)

    def annotate(self, stage_id, epoch, **fields):
        """Record runner evidence without changing admission or ownership."""
        with self.transaction():
            stage = self.stage(stage_id)
            if stage["epoch"] != epoch or stage["state"] not in OWNING:
                raise ResourceError("stale runner evidence")
            allowed = {"command_identity", "command_group", "command_index", "results"}
            if set(fields) - allowed:
                raise ResourceError("unsupported runner evidence field")
            stage.update(fields)
            self.save(stage)

    def admit(self, stage_id, token):
        with self.transaction():
            stage = self.stage(stage_id)
            if (
                stage["state"] != "launching"
                or stage.get("cancel_requested")
                or not secrets.compare_digest(stage.get("launch_token", ""), token)
            ):
                raise ResourceError("revoked or consumed launch token")
            stage.update(state="running", started_at=self.clock())
            self.save(stage)
            self.event(stage_id, "admitted", epoch=stage["epoch"])
            return stage

    def cancel(self, request_id):
        with self.transaction():
            self.request(request_id)
            for stage in self.stages(request_id):
                if stage["state"] == "waiting":
                    stage.update(state="cancelled", finished_at=self.clock())
                    stage.pop("protection", None)
                elif stage["state"] in OWNING:
                    stage["cancel_requested"] = True
                self.save(stage)
            self.event(request_id, "cancel_requested")
            self._outcomes([request_id])

    def subscribe(self, request_id, contract_digest, subscriber, *, remove=False):
        validate_subscriber(subscriber)
        with self.transaction():
            request = self.request(request_id)
            if digest(request["plan"]) != contract_digest:
                raise ResourceError("subscriber verification contract differs")
            identifier(subscriber["id"])
            current = next(
                (s for s in request["subscribers"] if s["id"] == subscriber["id"]),
                None,
            )
            if current is not None and not same_subscriber(current, subscriber):
                raise ResourceError(
                    "subscription identity already has a pinned destination"
                )
            history = self.db.execute(
                "SELECT generation,body,active FROM subscriber_history "
                "WHERE request=? AND subscriber=? ORDER BY generation DESC LIMIT 1",
                (request_id, subscriber["id"]),
            ).fetchone()
            if history is None and current is not None:
                self.db.execute(
                    "INSERT INTO subscriber_history VALUES (?,?,?,?,1)",
                    (request_id, subscriber["id"], 1, canonical(current)),
                )
                history = (1, canonical(current), 1)
            if history is not None and not same_subscriber(
                json.loads(history[1]), subscriber
            ):
                raise ResourceError(
                    "subscription identity already has a pinned destination"
                )
            if remove:
                request["subscribers"] = [
                    s for s in request["subscribers"] if s["id"] != subscriber["id"]
                ]
                self.db.execute(
                    "UPDATE subscriber_history SET active=0 "
                    "WHERE request=? AND subscriber=? AND active=1",
                    (request_id, subscriber["id"]),
                )
            elif current is None:
                request["subscribers"].append(subscriber)
                generation = int(history[0]) + 1 if history is not None else 1
                self.db.execute(
                    "UPDATE subscriber_history SET active=0 "
                    "WHERE request=? AND subscriber=?",
                    (request_id, subscriber["id"]),
                )
                self.db.execute(
                    "INSERT INTO subscriber_history VALUES (?,?,?,?,1)",
                    (
                        request_id,
                        subscriber["id"],
                        generation,
                        canonical(subscriber),
                    ),
                )
            self.db.execute(
                "UPDATE requests SET body=? WHERE id=?",
                (canonical(request), request_id),
            )
            self.event(request_id, "unsubscribed" if remove else "subscribed")
            self._outcomes([request_id])

    def abort_unadmitted(self, stage_id, epoch, *, token=None, identity=None):
        """Release only an authenticated intent that cannot have admitted work."""
        with self.transaction():
            stage = self.stage(stage_id)
            if stage["state"] != "launching" or stage["epoch"] != epoch:
                return False
            if token:
                if not stage.get("cancel_requested") or not secrets.compare_digest(
                    stage.get("launch_token", ""), token
                ):
                    return False
            elif not worker_lease.identity_matches(
                stage.get("supervisor_identity"), identity
            ):
                return False
            self._finish(
                stage_id,
                epoch,
                "cancelled" if token else "failed",
                list(stage["allocation"]),
                {"not_started": "admission revoked"},
            )
            return True

    def finish(self, stage_id, epoch, outcome, released, evidence):
        if outcome not in TERMINAL:
            raise ResourceError("invalid terminal outcome")
        with self.transaction():
            self._finish(stage_id, epoch, outcome, released, evidence)

    def _finish(self, stage_id, epoch, outcome, released, evidence):
        stage = self.stage(stage_id)
        if stage["epoch"] != epoch or stage["state"] not in OWNING:
            raise ResourceError("stale completion or already released allocation")
        if set(released) - set(stage["allocation"]):
            raise ResourceError("release contains unowned resource")
        registry = self.get("registry")
        released_groups = {registry[leaf].get("recovery_group") for leaf in released}
        if any(
            registry[leaf].get("recovery_group") in released_groups
            and registry[leaf].get("recovery_group")
            and leaf not in released
            for leaf in stage["allocation"]
        ):
            raise ResourceError("coupled recovery members must release together")
        allocation = {k: v for k, v in stage["allocation"].items() if k not in released}
        stage.update(
            state="recovery_required" if allocation else outcome,
            outcome=outcome,
            allocation=allocation,
            evidence=evidence,
            finished_at=self.clock(),
        )
        stage.pop("launch_token", None)
        stage.pop("maintenance_token", None)
        self.save(stage)
        self.put("released", sorted(set(self.get("released")) | set(released)))
        self.event(
            stage_id,
            "finished",
            outcome=outcome,
            released=released,
            evidence=evidence,
        )
        self._outcomes([stage["request"]])

    def recover(self, stage_id, epoch, released, evidence):
        stage = self.stage(stage_id)
        if (
            stage["state"] != "recovery_required"
            or evidence.get("quiescent") is not True
        ):
            raise ResourceError(
                "recovery requires exact quarantined owner and quiescence evidence"
            )
        self.finish(stage_id, epoch, stage.get("outcome", "failed"), released, evidence)

    def _outcomes(self, request_ids):
        for request_id in request_ids:
            stages = self.stages(request_id)
            recovery = any(s["state"] == "recovery_required" for s in stages)
            if not recovery and not all(s["state"] in TERMINAL for s in stages):
                continue
            request = self.request(request_id)
            status = (
                "passed" if all(s["state"] == "passed" for s in stages) else "failed"
            )
            if recovery:
                status = "action_required"
            elif all(s["state"] in {"cancelled", "skipped"} for s in stages):
                status = "cancelled"
            outcome_id = digest(
                [
                    status,
                    sorted(
                        (s["id"], s["epoch"])
                        for s in stages
                        if s["state"] == "recovery_required"
                    ),
                ]
            )
            for subscriber in request["subscribers"]:
                history = self.db.execute(
                    "SELECT generation FROM subscriber_history "
                    "WHERE request=? AND subscriber=? AND active=1 "
                    "ORDER BY generation DESC LIMIT 1",
                    (request_id, subscriber["id"]),
                ).fetchone()
                generation = int(history[0]) if history is not None else 1
                if history is None:
                    self.db.execute(
                        "INSERT OR IGNORE INTO subscriber_history VALUES (?,?,?,?,1)",
                        (
                            request_id,
                            subscriber["id"],
                            generation,
                            canonical(subscriber),
                        ),
                    )
                event_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        canonical(
                            [request_id, subscriber["id"], generation, outcome_id]
                        ),
                    )
                )
                value = {
                    "request": request_id,
                    "project": request["project"],
                    "external": request["external"],
                    "status": status,
                    "outcome_id": outcome_id,
                    "subscriber": subscriber,
                    "subscriber_generation": generation,
                    "stages": [
                        {k: v for k, v in s.items() if k not in PRIVATE_STAGE_FIELDS}
                        for s in stages
                    ],
                }
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox(id,request,body) VALUES (?,?,?)",
                    (event_id, request_id, canonical(value)),
                )

    def metrics(self, project):
        from .resource_metrics import report

        return report(self, project)

    def snapshot(self, project, request_id=None):
        if request_id and self.request(request_id)["project"] != project:
            raise ResourceError("request belongs to another project")
        stages = (
            self.stages(request_id, project=project)
            if request_id
            else self.stages(active=True, project=project)
        )
        stages = [
            {k: v for k, v in s.items() if k not in PRIVATE_STAGE_FIELDS}
            for s in stages
            if s["project"] == project
        ]
        delivery = [
            dict(row)
            for row in self.db.execute(
                "SELECT o.id,o.delivered,o.attempts,o.last_error FROM outbox AS o "
                "JOIN requests AS r ON o.request=r.id WHERE r.project=? "
                "AND (? IS NULL OR r.id=?)",
                (project, request_id, request_id),
            )
        ]
        return {
            "kind": "ORCHESTRATOR_RESOURCE_QUEUE",
            "schema_version": 1,
            "authority": self.get("authority"),
            "stages": stages,
            "delivery": delivery,
            "terminal": bool(stages) and all(s["state"] in TERMINAL for s in stages),
            "action_required": any(s["state"] == "recovery_required" for s in stages)
            or any(d["last_error"] and not d["delivered"] for d in delivery),
        }
