"""Resource ownership measurements; elapsed time is never model usage."""

from __future__ import annotations

import json


def intervals_union(intervals):
    end = None
    total = 0.0
    for start, finish in sorted(intervals):
        total += max(0, finish - max(start, end if end is not None else start))
        end = max(finish, end if end is not None else finish)
    return total


def report(ledger, project):
    now = ledger.clock()
    stages = ledger.stages(project=project)
    ids = {s["id"] for s in stages}
    waits, launch = [], []
    reasons, wait_seconds = {}, {}
    oldest = []
    for stage in stages:
        for key, samples in (("granted_at", waits), ("started_at", launch)):
            start_key = "ready_at" if key == "granted_at" else "granted_at"
            if key in stage and start_key in stage and stage[key] >= stage[start_key]:
                samples.append(stage[key] - stage[start_key])
        measured = dict(stage.get("wait_seconds", {}))
        if stage["state"] == "waiting":
            reason = stage["reason"]
            reasons[reason] = reasons.get(reason, 0) + 1
            delta = now - stage.get("transition_at", stage["created_at"])
            previous = measured.get(reason, 0)
            measured[reason] = (
                previous + delta if delta >= 0 and previous is not None else None
            )
            if "ready_seq" in stage and now >= stage["ready_at"]:
                oldest.append(now - stage["ready_at"])
        for reason, seconds in measured.items():
            previous = wait_seconds.get(reason, 0)
            wait_seconds[reason] = (
                previous + seconds
                if previous is not None and seconds is not None
                else None
            )
    owned = {}
    intervals = {}
    units = {}
    releases = {}
    release_latencies = {}
    invalid_clock = False
    first = min((s["created_at"] for s in stages), default=None)
    for row in ledger.db.execute(
        "SELECT at,subject,kind,body FROM events ORDER BY seq"
    ):
        if row["subject"] not in ids:
            continue
        at, subject, kind = row["at"], row["subject"], row["kind"]
        body = json.loads(row["body"])
        if kind == "granted":
            for leaf, claim in body["allocation"].items():
                weight = (
                    body.get("capacities", {}).get(leaf, 1)
                    if claim["mode"] == "exclusive"
                    else claim["units"]
                )
                owned[subject, leaf] = (at, weight)
                if leaf in releases and at >= releases[leaf]:
                    release_latencies.setdefault(leaf, []).append(at - releases[leaf])
        elif kind == "finished":
            for leaf in body["released"]:
                start, weight = owned.pop((subject, leaf), (at, 0))
                if at < start:
                    invalid_clock = True
                    continue
                intervals.setdefault(leaf, []).append((start, at))
                units[leaf] = units.get(leaf, 0) + weight * (at - start)
                releases[leaf] = at
    for (_subject, leaf), (start, weight) in owned.items():
        if now < start:
            invalid_clock = True
            continue
        intervals.setdefault(leaf, []).append((start, now))
        units[leaf] = units.get(leaf, 0) + weight * (now - start)
    resources = {}
    window = now - first if first is not None and now >= first else None
    for leaf, spans in intervals.items():
        busy = intervals_union(spans)
        latencies = release_latencies.get(leaf, [])
        resources[leaf] = {
            "owned_wall_seconds": None if invalid_clock else busy,
            "owned_capacity_unit_seconds": None if invalid_clock else units[leaf],
            "ownership_fraction_of_observation": busy / window if window else None,
            "release_to_next_grant_samples": len(latencies),
            "mean_release_to_next_grant_seconds": sum(latencies) / len(latencies)
            if latencies
            else None,
        }
    return {
        "kind": "ORCHESTRATOR_RESOURCE_METRICS",
        "schema_version": 1,
        "sample_size": len(stages),
        "observation_seconds": window,
        "ready_to_grant_samples": len(waits),
        "mean_ready_to_grant_seconds": sum(waits) / len(waits) if waits else None,
        "grant_to_launch_samples": len(launch),
        "mean_grant_to_launch_seconds": sum(launch) / len(launch) if launch else None,
        "oldest_ready_wait_seconds": max(oldest, default=None),
        "waiting_reasons": reasons,
        "stage_wait_seconds_by_reason": wait_seconds,
        "bypass_count": sum(s.get("bypass_count", 0) for s in stages),
        "recovery_required": sum(s["state"] == "recovery_required" for s in stages),
        "protected_requests": sum(bool(s.get("protection")) for s in stages),
        "resources": resources,
        "clock_discontinuity": invalid_clock,
        "coverage": "registered project attempts; ownership includes quarantine",
        "agent_active_seconds": None,
        "eligible_idle_seconds": None,
        "protection_idle_seconds": None,
        "limitations": [
            "Ownership is not CPU utilization or useful work.",
            "Stage and capacity-unit seconds may overlap.",
            "Idle attribution requires continuous executor availability evidence.",
            "Other projects' private timelines are excluded.",
        ],
    }
