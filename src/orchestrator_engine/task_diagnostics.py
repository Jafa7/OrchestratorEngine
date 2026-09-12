"""Read-only diagnostics for detached worker task artifacts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    core,
    platform_runtime,
    task_resolution,
    worker_diagnostics,
    workers,
)

TASK_DIAGNOSTICS_KIND = "WORKER_TASK_DIAGNOSTICS"
RUNNING_STATUSES = {"starting", "running", "cancelling"}
QUEUED_STATUSES = {"queued"}
UNSUCCESSFUL_TERMINAL_STATUSES = core.TERMINAL_STATUSES - {"completed"}
DEFAULT_STALE_AFTER_SECONDS = workers.TASK_HEARTBEAT_INTERVAL_SECONDS * 3
DEFAULT_LARGE_LOG_BYTES = 1024 * 1024
TASK_STATUSES = RUNNING_STATUSES | QUEUED_STATUSES | core.TERMINAL_STATUSES
ProcessChecker = Callable[[int], bool]
VERIFICATION_LEVEL_RANK = {"structural": 0, "focused": 1, "full": 2}
TASK_INDEX_SCHEMA_VERSION = 2
MAX_COMPACT_RESOLVED_TASKS = 32


class TaskDiagnosticError(RuntimeError):
    """A deterministic worker task diagnostic failure."""


def verification_acceptance(
    *,
    task_id: str,
    status: str,
    task_dir: Path,
    descriptor: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    intent = descriptor.get("task_intent")
    requested = intent.get("verification") if isinstance(intent, dict) else None
    if requested not in VERIFICATION_LEVEL_RANK:
        return {"status": "not_declared", "requested_verification": None}, []
    base = {"requested_verification": requested}
    if status != "completed":
        return {**base, "status": "not_applicable"}, []

    handoff_path = task_dir / "worker-handoff.json"
    if not handoff_path.is_file():
        return {**base, "status": "evidence_missing"}, [
            diagnostic(
                code="task_acceptance_evidence_missing",
                severity="warning",
                message=(
                    f"task {task_id} completed without verification handoff "
                    f"evidence for the requested {requested} level"
                ),
                suggested_action=(
                    "Inspect the worker result and checks before accepting the "
                    "task; process completion alone is not quality evidence."
                ),
            )
        ]
    try:
        handoff = core.load_object(handoff_path)
        workers.validate_worker_handoff(handoff)
    except (OSError, core.OrchestratorError, workers.WorkerError) as error:
        return {**base, "status": "evidence_invalid"}, [
            diagnostic(
                code="task_acceptance_evidence_invalid",
                severity="warning",
                message=f"task {task_id} verification handoff is invalid: {error}",
                suggested_action=(
                    "Inspect the worker result and obtain valid bounded "
                    "verification evidence before acceptance."
                ),
            )
        ]

    verification = handoff.get("verification")
    if not isinstance(verification, dict):
        return {**base, "status": "evidence_missing"}, [
            diagnostic(
                code="task_acceptance_evidence_missing",
                severity="warning",
                message=(
                    f"task {task_id} handoff does not declare verification "
                    f"evidence for the requested {requested} level"
                ),
                suggested_action=(
                    "Inspect the actual checks before accepting the task or "
                    "record an operator resolution with the evidence reviewed."
                ),
            )
        ]

    reported = str(verification["level"])
    verification_status = str(verification["status"])
    checks = verification["checks"]
    assessment = {
        **base,
        "reported_verification": reported,
        "verification_status": verification_status,
        "check_count": len(checks),
    }
    if VERIFICATION_LEVEL_RANK[reported] < VERIFICATION_LEVEL_RANK[requested]:
        return {**assessment, "status": "below_required_level"}, [
            diagnostic(
                code="task_acceptance_verification_below_intent",
                severity="warning",
                message=(
                    f"task {task_id} reports {reported} verification below "
                    f"the requested {requested} level"
                ),
                suggested_action=(
                    "Run or request the missing verification before accepting "
                    "the worker result."
                ),
            )
        ]
    passed_checks = sum(check.get("status") == "passed" for check in checks)
    if (
        verification_status != "passed"
        or not checks
        or passed_checks == 0
        or any(check.get("status") == "failed" for check in checks)
    ):
        return {**assessment, "status": "evidence_incomplete"}, [
            diagnostic(
                code="task_acceptance_evidence_incomplete",
                severity="warning",
                message=(
                    f"task {task_id} process completed but its verification "
                    "handoff does not establish acceptance"
                ),
                suggested_action=(
                    "Inspect failed, skipped or missing checks and complete the "
                    "required verification before acceptance."
                ),
            )
        ]
    return {**assessment, "status": "evidenced"}, []


def process_alive(pid: int) -> bool:
    return platform_runtime.process_alive(pid)


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def age_seconds(value: object, *, now: datetime) -> float | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return max((now - parsed).total_seconds(), 0.0)


def diagnostic(
    *,
    code: str,
    severity: str,
    message: str,
    suggested_action: str,
) -> dict[str, str]:
    return worker_diagnostics.diagnostic(
        code=code,
        severity=severity,
        message=message,
        suggested_action=suggested_action,
    )


def diagnose_tasks(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    task_id: str | None = None,
    worker: str | None = None,
    status: str | None = None,
    minimum_severity: str = "info",
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    large_log_bytes: int = DEFAULT_LARGE_LOG_BYTES,
    process_checker: ProcessChecker = process_alive,
    now: datetime | None = None,
    compact_indexed: bool = False,
) -> dict[str, Any]:
    if stale_after_seconds <= 0:
        raise TaskDiagnosticError("stale_after_seconds must be positive")
    if large_log_bytes <= 0:
        raise TaskDiagnosticError("large_log_bytes must be positive")
    project = project_root.expanduser().resolve()
    current = now or datetime.now(UTC)
    if compact_indexed and task_id is None and worker is None and status is None:
        return _diagnose_tasks_indexed(
            project,
            state_dir=state_dir,
            minimum_severity=minimum_severity,
            stale_after_seconds=stale_after_seconds,
            large_log_bytes=large_log_bytes,
            process_checker=process_checker,
            now=current,
        )
    task_paths = selected_task_paths(project, state_dir=state_dir, task_id=task_id)

    summaries: dict[str, Any] = {}
    all_diagnostics: list[dict[str, str]] = []
    for descriptor_path in task_paths:
        summary = summarize_task(
            project,
            descriptor_path,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
            large_log_bytes=large_log_bytes,
            process_checker=process_checker,
            now=current,
        )
        if status is not None and summary.get("status") != status:
            continue
        if worker is not None and summary.get("worker") != worker:
            continue
        filtered = worker_diagnostics.filter_diagnostics(
            summary["diagnostics"],
            minimum_severity=minimum_severity,
        )
        summary["diagnostics"] = filtered
        summary["diagnostic_count"] = len(filtered)
        summary["severity_counts"] = worker_diagnostics.severity_counts(filtered)
        summary["worst_severity"] = worker_diagnostics.worst_severity(filtered)
        all_diagnostics.extend(filtered)
        summaries[str(summary["directory_task_id"])] = summary

    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": TASK_DIAGNOSTICS_KIND,
        "tasks_root": str(workers.tasks_root(project, state_dir=state_dir)),
        "generated_at": core.utc_now(),
        "filters": {
            "task_id": task_id,
            "worker": worker,
            "status": status,
            "minimum_severity": minimum_severity,
            "stale_after_seconds": stale_after_seconds,
            "large_log_bytes": large_log_bytes,
        },
        "task_count": len(summaries),
        "status_counts": status_counts(summaries.values()),
        "resolution_counts": resolution_counts(summaries.values()),
        "diagnostic_count": len(all_diagnostics),
        "severity_counts": worker_diagnostics.severity_counts(all_diagnostics),
        "worst_severity": worker_diagnostics.worst_severity(all_diagnostics),
        "tasks": summaries,
    }


def _diagnose_tasks_indexed(
    project_root: Path,
    *,
    state_dir: str,
    minimum_severity: str,
    stale_after_seconds: float,
    large_log_bytes: int,
    process_checker: ProcessChecker,
    now: datetime,
) -> dict[str, Any]:
    state_root = core.state_root(project_root, state_dir=state_dir)
    index_path = state_root / "indexes" / "task-diagnostics.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index_path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summaries(
                task_id TEXT PRIMARY KEY,
                descriptor_path TEXT NOT NULL,
                status TEXT NOT NULL,
                worker TEXT,
                resolution_status TEXT,
                info_count INTEGER NOT NULL,
                warning_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL,
                max_log_bytes INTEGER NOT NULL,
                summary_json TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_summary_status_idx
                ON summaries(status, task_id);
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(summaries)")
        }
        if "max_log_bytes" not in columns:
            connection.execute(
                "ALTER TABLE summaries ADD COLUMN max_log_bytes INTEGER "
                "NOT NULL DEFAULT 0"
            )
            connection.execute("DELETE FROM summaries")
            connection.execute(
                "DELETE FROM metadata WHERE key IN ('schema_version','journal_offset')"
            )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        discovery = _refresh_task_index(
            connection,
            project_root,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
            large_log_bytes=large_log_bytes,
            process_checker=process_checker,
            now=now,
        )
        recovery_markers = discovery.pop("_recovery_markers", [])
        status_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM summaries GROUP BY status"
        ).fetchall()
        resolution_rows = connection.execute(
            """SELECT resolution_status, COUNT(*) AS count FROM summaries
               WHERE resolution_status IS NOT NULL GROUP BY resolution_status"""
        ).fetchall()
        severity_column = {
            "info": "info_count + warning_count + error_count",
            "warning": "warning_count + error_count",
            "error": "error_count",
        }[minimum_severity]
        selected = connection.execute(
            f"""SELECT * FROM summaries
                WHERE status IN ('starting','running','cancelling','queued')
                   OR (({severity_column}) > 0 AND resolution_status IS NULL)
                   OR max_log_bytes > ?
                ORDER BY task_id""",
            (large_log_bytes,),
        ).fetchall()
        resolved = connection.execute(
            """SELECT * FROM summaries WHERE resolution_status IS NOT NULL
               ORDER BY indexed_at DESC, task_id LIMIT ?""",
            (MAX_COMPACT_RESOLVED_TASKS,),
        ).fetchall()
        totals = connection.execute(
            f"""SELECT COUNT(*) AS task_count,
                       COALESCE(SUM({severity_column}), 0) AS diagnostic_count,
                       COALESCE(SUM(info_count), 0) AS info_count,
                       COALESCE(SUM(warning_count), 0) AS warning_count,
                       COALESCE(SUM(error_count), 0) AS error_count
                FROM summaries"""
        ).fetchone()
        connection.commit()
        core.clear_index_recovery_markers(recovery_markers)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    summaries: dict[str, Any] = {}
    for row in [*selected, *resolved]:
        summary = json.loads(row["summary_json"])
        diagnostics = worker_diagnostics.filter_diagnostics(
            summary.get("diagnostics", []), minimum_severity=minimum_severity
        )
        summary["diagnostics"] = diagnostics
        summary["diagnostic_count"] = len(diagnostics)
        summary["severity_counts"] = worker_diagnostics.severity_counts(diagnostics)
        summary["worst_severity"] = worker_diagnostics.worst_severity(diagnostics)
        summaries[str(row["task_id"])] = summary
    severity_counts = {
        severity: int(totals[f"{severity}_count"])
        for severity in worker_diagnostics.SEVERITIES
    }
    filtered_severity_counts = {
        severity: count
        for severity, count in severity_counts.items()
        if worker_diagnostics.SEVERITY_RANK[severity]
        >= worker_diagnostics.SEVERITY_RANK[minimum_severity]
    }
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": TASK_DIAGNOSTICS_KIND,
        "tasks_root": str(workers.tasks_root(project_root, state_dir=state_dir)),
        "generated_at": core.utc_now(),
        "filters": {
            "task_id": None,
            "worker": None,
            "status": None,
            "minimum_severity": minimum_severity,
            "stale_after_seconds": stale_after_seconds,
            "large_log_bytes": large_log_bytes,
            "compact_indexed": True,
        },
        "task_count": int(totals["task_count"]),
        "status_counts": {
            **{item: 0 for item in sorted(TASK_STATUSES)},
            **{str(row["status"]): int(row["count"]) for row in status_rows},
        },
        "resolution_counts": {
            **{item: 0 for item in sorted(task_resolution.RESOLUTION_STATUSES)},
            **{
                str(row["resolution_status"]): int(row["count"])
                for row in resolution_rows
            },
        },
        "diagnostic_count": int(totals["diagnostic_count"]),
        "severity_counts": filtered_severity_counts,
        "worst_severity": worker_diagnostics.worst_severity(
            [
                {"severity": severity}
                for severity, count in filtered_severity_counts.items()
                if count
            ]
        ),
        "tasks": summaries,
        "task_view": {
            "included_count": len(summaries),
            "active_or_unresolved_count": len(selected),
            "resolved_included_count": len(resolved),
            "resolved_limit": MAX_COMPACT_RESOLVED_TASKS,
        },
        "discovery": discovery,
    }


def status_counts(summaries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(TASK_STATUSES)}
    unknown_count = 0
    for summary in summaries:
        status = summary.get("status")
        if isinstance(status, str) and status in counts:
            counts[status] += 1
        else:
            unknown_count += 1
    if unknown_count:
        counts["unknown"] = unknown_count
    return counts


def resolution_counts(summaries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(task_resolution.RESOLUTION_STATUSES)}
    for summary in summaries:
        resolution = summary.get("resolution")
        if not isinstance(resolution, dict):
            continue
        status = resolution.get("status")
        if isinstance(status, str) and status in counts:
            counts[status] += 1
    return counts


def selected_task_paths(
    project_root: Path,
    *,
    state_dir: str,
    task_id: str | None,
) -> list[Path]:
    root = workers.tasks_root(project_root, state_dir=state_dir)
    if task_id is not None:
        if not task_id or "/" in task_id or "\\" in task_id or task_id.startswith("."):
            raise TaskDiagnosticError(f"invalid task id: {task_id!r}")
        path = root / task_id / "task.json"
        if not path.exists() and not path.parent.exists():
            raise TaskDiagnosticError(f"unknown task: {task_id}")
        return [path]
    if not root.is_dir():
        return []
    return [
        task_dir / "task.json"
        for task_dir in sorted(root.iterdir())
        if task_dir.is_dir()
    ]


def _refresh_task_index(
    connection: sqlite3.Connection,
    project_root: Path,
    *,
    state_dir: str,
    stale_after_seconds: float,
    large_log_bytes: int,
    process_checker: ProcessChecker,
    now: datetime,
) -> dict[str, Any]:
    state_root = core.state_root(project_root, state_dir=state_dir)
    recovery = core.index_recovery_markers(
        state_root, index_name="task-diagnostics"
    )
    recoverable_markers = [*recovery["ready"], *recovery["invalid"]]
    if recoverable_markers:
        connection.execute("DELETE FROM summaries")
        connection.execute("DELETE FROM metadata")
    journal = state_root / "indexes" / "task-diagnostics.jsonl"
    bootstrapped = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    rebuilt = bootstrapped is None
    rebuild_paths_examined = 0
    if rebuilt:
        initial_offset = journal.stat().st_size if journal.is_file() else 0
        root = workers.tasks_root(project_root, state_dir=state_dir)
        if root.is_dir():
            for task_dir in root.iterdir():
                if not task_dir.is_dir():
                    continue
                rebuild_paths_examined += 1
                _index_task_summary(
                    connection,
                    project_root,
                    descriptor_path=task_dir / "task.json",
                    state_dir=state_dir,
                    stale_after_seconds=stale_after_seconds,
                    large_log_bytes=large_log_bytes,
                    process_checker=process_checker,
                    now=now,
                )
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
            (str(TASK_INDEX_SCHEMA_VERSION),),
        )
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('journal_offset',?)",
            (str(initial_offset),),
        )
    elif bootstrapped["value"] != str(TASK_INDEX_SCHEMA_VERSION):
        connection.execute("DELETE FROM summaries")
        connection.execute("DELETE FROM metadata")
        return _refresh_task_index(
            connection,
            project_root,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
            large_log_bytes=large_log_bytes,
            process_checker=process_checker,
            now=now,
        )

    offset_row = connection.execute(
        "SELECT value FROM metadata WHERE key='journal_offset'"
    ).fetchone()
    offset = int(offset_row["value"]) if offset_row is not None else 0
    changed_task_ids: set[str] = set()
    journal_records_examined = 0
    if journal.is_file():
        size = journal.stat().st_size
        if offset > size:
            connection.execute("DELETE FROM summaries")
            connection.execute("DELETE FROM metadata")
            return _refresh_task_index(
                connection,
                project_root,
                state_dir=state_dir,
                stale_after_seconds=stale_after_seconds,
                large_log_bytes=large_log_bytes,
                process_checker=process_checker,
                now=now,
            )
        with journal.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    handle.seek(line_start)
                    break
                offset = handle.tell()
                journal_records_examined += 1
                try:
                    value = json.loads(line)
                    task_id = _task_id_from_journal_path(
                        state_root, Path(value["path"])
                    )
                except (
                    KeyError,
                    OSError,
                    TypeError,
                    ValueError,
                    core.OrchestratorError,
                    json.JSONDecodeError,
                ):
                    continue
                if task_id is not None:
                    changed_task_ids.add(task_id)
    active_task_ids = {
        str(row["task_id"])
        for row in connection.execute(
            """SELECT task_id FROM summaries
               WHERE status IN ('starting','running','cancelling','queued')"""
        ).fetchall()
    }
    refreshed = changed_task_ids | active_task_ids
    for task_id in sorted(refreshed):
        descriptor_path = (
            workers.tasks_root(project_root, state_dir=state_dir)
            / task_id
            / "task.json"
        )
        if not descriptor_path.parent.is_dir():
            connection.execute("DELETE FROM summaries WHERE task_id=?", (task_id,))
            continue
        _index_task_summary(
            connection,
            project_root,
            descriptor_path=descriptor_path,
            state_dir=state_dir,
            stale_after_seconds=stale_after_seconds,
            large_log_bytes=large_log_bytes,
            process_checker=process_checker,
            now=now,
        )
    connection.execute(
        """INSERT INTO metadata(key,value) VALUES('journal_offset',?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (str(offset),),
    )
    return {
        "index_rebuilt": rebuilt,
        "rebuild_paths_examined": rebuild_paths_examined,
        "journal_records_examined": journal_records_examined,
        "active_rows_refreshed": len(active_task_ids),
        "changed_rows_refreshed": len(changed_task_ids),
        "index_path": str(state_root / "indexes" / "task-diagnostics.sqlite3"),
        "coverage": (
            "recovery_pending"
            if recovery["pending"]
            else "all_indexed_tasks_with_active_and_changed_rows_refreshed"
        ),
        "recovery_rebuilt": bool(recoverable_markers),
        "pending_recovery_markers": len(recovery["pending"]),
        "invalid_recovery_markers": len(recovery["invalid"]),
        "_recovery_markers": recoverable_markers,
    }


def _task_id_from_journal_path(state_root: Path, path: Path) -> str | None:
    resolved_state = state_root.resolve()
    resolved = path.expanduser().resolve()
    relative = resolved.relative_to(resolved_state)
    parts = relative.parts
    if len(parts) == 3 and parts[0] == "tasks":
        return core.validate_event_id(parts[1])
    if (
        len(parts) == 2
        and parts[0] == "task-resolutions"
        and resolved.suffix == ".json"
    ):
        return core.validate_event_id(resolved.stem)
    return None


def _index_task_summary(
    connection: sqlite3.Connection,
    project_root: Path,
    *,
    descriptor_path: Path,
    state_dir: str,
    stale_after_seconds: float,
    large_log_bytes: int,
    process_checker: ProcessChecker,
    now: datetime,
) -> None:
    summary = summarize_task(
        project_root,
        descriptor_path,
        state_dir=state_dir,
        stale_after_seconds=stale_after_seconds,
        large_log_bytes=large_log_bytes,
        process_checker=process_checker,
        now=now,
    )
    task_id = str(summary["directory_task_id"])
    counts = worker_diagnostics.severity_counts(summary.get("diagnostics", []))
    resolution = summary.get("resolution")
    resolution_status = (
        str(resolution.get("status")) if isinstance(resolution, dict) else None
    )
    connection.execute(
        """INSERT INTO summaries(
               task_id, descriptor_path, status, worker, resolution_status,
               info_count, warning_count, error_count, max_log_bytes,
               summary_json, indexed_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(task_id) DO UPDATE SET
             descriptor_path=excluded.descriptor_path,
             status=excluded.status,
             worker=excluded.worker,
             resolution_status=excluded.resolution_status,
             info_count=excluded.info_count,
             warning_count=excluded.warning_count,
             error_count=excluded.error_count,
             max_log_bytes=excluded.max_log_bytes,
             summary_json=excluded.summary_json,
             indexed_at=excluded.indexed_at""",
        (
            task_id,
            str(descriptor_path),
            str(summary.get("status") or "unknown"),
            summary.get("worker"),
            resolution_status,
            int(counts.get("info", 0)),
            int(counts.get("warning", 0)),
            int(counts.get("error", 0)),
            max(
                (
                    size
                    for size in summary.get("log_sizes", {}).values()
                    if isinstance(size, int)
                ),
                default=0,
            ),
            json.dumps(summary, sort_keys=True, separators=(",", ":")),
            core.utc_now(),
        ),
    )


def summarize_task(
    project_root: Path,
    descriptor_path: Path,
    *,
    state_dir: str,
    stale_after_seconds: float,
    large_log_bytes: int,
    process_checker: ProcessChecker,
    now: datetime,
) -> dict[str, Any]:
    task_dir = descriptor_path.parent
    descriptor: dict[str, Any]
    diagnostics: list[dict[str, str]] = []
    try:
        descriptor = core.load_object(descriptor_path)
    except (OSError, core.OrchestratorError) as error:
        task_id = task_dir.name
        resolution = load_task_resolution(
            project_root,
            task_id,
            state_dir=state_dir,
            diagnostics=diagnostics,
        )
        diagnostics.append(
            diagnostic(
                code="task_descriptor_unreadable",
                severity="error",
                message=f"task {task_id} descriptor is unreadable: {error}",
                suggested_action=(
                    "Inspect or repair task.json manually; do not delete "
                    "durable task artifacts unless a retention rule allows it."
                ),
            )
        )
        return base_summary(
            task_id=task_id,
            descriptor_path=descriptor_path,
            task_dir=task_dir,
            diagnostics=diagnostics,
            resolution=resolution,
        )

    task_id = str(descriptor.get("task_id") or task_dir.name)
    status = str(descriptor.get("status") or "unknown")
    resolution = load_task_resolution(
        project_root,
        task_id,
        state_dir=state_dir,
        diagnostics=diagnostics,
    )
    artifacts = task_artifacts(task_dir, descriptor)
    log_sizes = log_artifact_sizes(artifacts)
    supervisor_pid = descriptor.get("supervisor_pid")
    worker_pid = descriptor.get("worker_pid")
    if status in RUNNING_STATUSES:
        supervisor_alive = (
            process_checker(supervisor_pid) if isinstance(supervisor_pid, int) else None
        )
        worker_alive = (
            process_checker(worker_pid) if isinstance(worker_pid, int) else None
        )
        heartbeat_age = age_seconds(
            descriptor.get("last_alive_at") or descriptor.get("created_at"),
            now=now,
        )
    else:
        supervisor_alive = None
        worker_alive = None
        heartbeat_age = None

    descriptor_items = descriptor_diagnostics(
        task_id=task_id,
        descriptor=descriptor,
        descriptor_path=descriptor_path,
        directory_task_id=task_dir.name,
        status=status,
    )
    diagnostics.extend(descriptor_items)
    diagnostics.extend(
        historical_profile_diagnostics(
            task_id=task_id,
            evidence_path=artifacts["evidence"],
            existing_codes={item["code"] for item in descriptor_items},
        )
    )
    diagnostics.extend(
        running_task_diagnostics(
            task_id=task_id,
            status=status,
            supervisor_pid=supervisor_pid,
            supervisor_alive=supervisor_alive,
            worker_pid=worker_pid,
            worker_alive=worker_alive,
            heartbeat_age=heartbeat_age,
            stale_after_seconds=stale_after_seconds,
        )
    )
    diagnostics.extend(
        terminal_task_diagnostics(
            task_id=task_id,
            status=status,
            task_dir=task_dir,
            artifacts=artifacts,
            resolution=resolution,
        )
    )
    diagnostics.extend(
        log_size_diagnostics(
            task_id=task_id,
            log_sizes=log_sizes,
            large_log_bytes=large_log_bytes,
        )
    )
    diagnostics.extend(
        runtime_policy_diagnostics(
            task_id=task_id,
            status=status,
            descriptor=descriptor,
            log_sizes=log_sizes,
            now=now,
        )
    )
    acceptance, acceptance_diagnostics = verification_acceptance(
        task_id=task_id,
        status=status,
        task_dir=task_dir,
        descriptor=descriptor,
    )
    diagnostics.extend(acceptance_diagnostics)
    diagnostics = apply_diagnostic_resolutions(diagnostics, resolution)

    summary = base_summary(
        task_id=task_id,
        descriptor_path=descriptor_path,
        task_dir=task_dir,
        diagnostics=diagnostics,
        resolution=resolution,
    )
    summary.update(
        {
            "worker": descriptor.get("worker"),
            "status": status,
            "created_at": descriptor.get("created_at"),
            "finished_at": descriptor.get("finished_at"),
            "last_alive_at": descriptor.get("last_alive_at"),
            "heartbeat_age_seconds": (
                round(heartbeat_age, 3) if heartbeat_age is not None else None
            ),
            "supervisor_pid": supervisor_pid,
            "supervisor_alive": supervisor_alive,
            "worker_pid": worker_pid,
            "worker_alive": worker_alive,
            "artifacts": {name: str(path) for name, path in artifacts.items()},
            "log_sizes": log_sizes,
            "progress": descriptor.get("progress"),
            "usage": descriptor.get("usage"),
            "runtime_policy": descriptor.get("runtime_policy", {}),
            "acceptance": acceptance,
        }
    )
    return summary


def apply_diagnostic_resolutions(
    diagnostics: list[dict[str, str]],
    resolution: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Downgrade explicitly resolved non-error diagnostics to info."""
    if not isinstance(resolution, dict):
        return diagnostics
    raw_codes = resolution.get("diagnostic_codes")
    if not isinstance(raw_codes, list):
        return diagnostics
    codes = {code for code in raw_codes if isinstance(code, str)}
    resolved: list[dict[str, str]] = []
    for item in diagnostics:
        if item.get("code") not in codes or item.get("severity") == "error":
            resolved.append(item)
            continue
        resolved.append(
            {
                **item,
                "severity": "info",
                "suggested_action": (
                    "Operator resolved this diagnostic in the durable task "
                    "resolution; inspect that resolution for the recorded reason."
                ),
            }
        )
    return resolved


def runtime_policy_diagnostics(
    *,
    task_id: str,
    status: str,
    descriptor: dict[str, Any],
    log_sizes: dict[str, int | None],
    now: datetime,
) -> list[dict[str, str]]:
    policy = descriptor.get("runtime_policy")
    if not isinstance(policy, dict):
        return []
    diagnostics: list[dict[str, str]] = []
    progress = descriptor.get("progress")
    no_progress_limit = policy.get("max_no_progress_seconds")
    if status in RUNNING_STATUSES and isinstance(no_progress_limit, (int, float)):
        stamp = (
            progress.get("last_output_growth_at")
            if isinstance(progress, dict)
            else descriptor.get("created_at")
        )
        no_progress_age = age_seconds(stamp, now=now)
        if no_progress_age is not None and no_progress_age > no_progress_limit:
            diagnostics.append(
                diagnostic(
                    code="task_no_output_growth",
                    severity="warning",
                    message=(
                        f"task {task_id} has no mechanical output growth for "
                        f"{no_progress_age:.1f}s (soft limit {no_progress_limit:g}s)"
                    ),
                    suggested_action=(
                        "Inspect the bounded log tail and worker process state; "
                        "do not cancel solely from this advisory."
                    ),
                )
            )
    duration_limit = policy.get("soft_duration_seconds")
    duration_end = parse_timestamp(descriptor.get("finished_at")) or now
    duration = age_seconds(descriptor.get("created_at"), now=duration_end)
    if (
        isinstance(duration_limit, (int, float))
        and duration is not None
        and duration > duration_limit
    ):
        diagnostics.append(
            diagnostic(
                code="task_soft_duration_exceeded",
                severity="info",
                message=(f"task {task_id} exceeded soft duration {duration_limit:g}s"),
                suggested_action=(
                    "Review progress; this soft budget never stops work."
                ),
            )
        )
    output_limit = policy.get("soft_output_bytes")
    output_bytes = sum(value or 0 for value in log_sizes.values())
    if isinstance(output_limit, (int, float)) and output_bytes > output_limit:
        diagnostics.append(
            diagnostic(
                code="task_soft_output_exceeded",
                severity="info",
                message=(
                    f"task {task_id} exceeded soft output budget {output_limit:g} bytes"
                ),
                suggested_action=(
                    "Use compact evidence and inspect full logs only as needed."
                ),
            )
        )
    token_limit = policy.get("soft_token_budget")
    usage = descriptor.get("usage")
    total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    measurement_status = (
        usage.get("measurement_status") if isinstance(usage, dict) else None
    )
    if (
        status in core.TERMINAL_STATUSES
        and isinstance(token_limit, (int, float))
        and measurement_status != "complete"
    ):
        diagnostics.append(
            diagnostic(
                code="task_usage_measurement_incomplete",
                severity="info",
                message=(
                    f"task {task_id} usage measurement is "
                    f"{measurement_status or 'unavailable'}"
                ),
                suggested_action=(
                    "Do not compare this task's token total or tune profiles "
                    "from it; configure a provider-format usage adapter."
                ),
            )
        )
    elif (
        status in core.TERMINAL_STATUSES
        and isinstance(token_limit, (int, float))
        and isinstance(total_tokens, int)
        and total_tokens > token_limit
    ):
        token_counts = usage.get("token_counts")
        cached_tokens = None
        if isinstance(token_counts, dict):
            direct_cached = token_counts.get("cached_input_tokens")
            split_cached = sum(
                value
                for key in (
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
                if isinstance(value := token_counts.get(key), int)
                and not isinstance(value, bool)
                and value >= 0
            )
            candidates = [split_cached]
            if (
                isinstance(direct_cached, int)
                and not isinstance(direct_cached, bool)
                and direct_cached >= 0
            ):
                candidates.append(direct_cached)
            cached_tokens = max(candidates)
        cached_detail = (
            f", including {cached_tokens} cached input tokens"
            if isinstance(cached_tokens, int) and cached_tokens > 0
            else ""
        )
        diagnostics.append(
            diagnostic(
                code="task_soft_token_budget_exceeded",
                severity="info",
                message=(
                    f"task {task_id} used {total_tokens} tokens{cached_detail} "
                    f"(soft budget {token_limit:g})"
                ),
                suggested_action=(
                    "Review future profile selection; the completed result "
                    "remains valid."
                ),
            )
        )
    return diagnostics


def historical_profile_diagnostics(
    *,
    task_id: str,
    evidence_path: Path,
    existing_codes: set[str],
) -> list[dict[str, str]]:
    if not evidence_path.is_file():
        return []
    try:
        evidence = core.load_object(evidence_path)
    except (OSError, core.OrchestratorError):
        return []
    command = evidence.get("command")
    worker_config = evidence.get("worker_config")
    prompt_via = (
        worker_config.get("prompt_via") if isinstance(worker_config, dict) else None
    )
    if (
        not isinstance(command, list)
        or not all(isinstance(item, str) for item in command)
        or not isinstance(prompt_via, str)
    ):
        return []
    return [
        item
        for item in workers.worker_profile_warnings(
            name=str(evidence.get("worker") or task_id),
            command=command,
            prompt_via=prompt_via,
        )
        if item["code"] not in existing_codes
    ]


def base_summary(
    *,
    task_id: str,
    descriptor_path: Path,
    task_dir: Path,
    diagnostics: list[dict[str, str]],
    resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "task_id": task_id,
        "directory_task_id": task_dir.name,
        "task_dir": str(task_dir),
        "descriptor_path": str(descriptor_path),
        "diagnostic_count": len(diagnostics),
        "severity_counts": worker_diagnostics.severity_counts(diagnostics),
        "worst_severity": worker_diagnostics.worst_severity(diagnostics),
        "diagnostics": diagnostics,
    }
    if resolution is not None:
        summary["resolution"] = resolution
    return summary


def load_task_resolution(
    project_root: Path,
    task_id: str,
    *,
    state_dir: str,
    diagnostics: list[dict[str, str]],
) -> dict[str, Any] | None:
    try:
        return task_resolution.load_resolution(
            project_root,
            task_id,
            state_dir=state_dir,
        )
    except (
        OSError,
        core.OrchestratorError,
        task_resolution.TaskResolutionError,
    ) as error:
        diagnostics.append(
            diagnostic(
                code="task_resolution_unreadable",
                severity="error",
                message=f"task {task_id} resolution is unreadable: {error}",
                suggested_action=(
                    "Inspect the task resolution file; do not delete task "
                    "result/evidence to hide this diagnostic."
                ),
            )
        )
    return None


def task_artifacts(task_dir: Path, descriptor: dict[str, Any]) -> dict[str, Path]:
    artifacts = {
        "result": path_from_descriptor(descriptor, "result_path")
        or task_dir / "result.json",
        "evidence": path_from_descriptor(descriptor, "evidence_path")
        or task_dir / "evidence.json",
        "stdout": task_dir / "worker-stdout.log",
        "stderr": task_dir / "worker-stderr.log",
        "supervisor_log": path_from_descriptor(descriptor, "supervisor_log")
        or task_dir / "supervisor.log",
    }
    for key in ("event_path", "signal_path"):
        path = path_from_descriptor(descriptor, key)
        if path is not None:
            artifacts[key.removesuffix("_path")] = path
    return artifacts


def log_artifact_sizes(artifacts: dict[str, Path]) -> dict[str, int | None]:
    sizes: dict[str, int | None] = {}
    for artifact_name in ("stdout", "stderr", "supervisor_log"):
        path = artifacts.get(artifact_name)
        if path is None or not path.is_file():
            sizes[artifact_name] = None
            continue
        try:
            sizes[artifact_name] = path.stat().st_size
        except OSError:
            sizes[artifact_name] = None
    return sizes


def path_from_descriptor(descriptor: dict[str, Any], key: str) -> Path | None:
    value = descriptor.get(key)
    if not isinstance(value, str) or not value:
        return None
    return Path(value).expanduser().resolve()


def descriptor_diagnostics(
    *,
    task_id: str,
    descriptor: dict[str, Any],
    descriptor_path: Path,
    directory_task_id: str,
    status: str,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if descriptor.get("task_id") != directory_task_id:
        diagnostics.append(
            diagnostic(
                code="task_id_mismatch",
                severity="error",
                message=(
                    f"task descriptor id {descriptor.get('task_id')!r} does "
                    f"not match directory {directory_task_id!r}"
                ),
                suggested_action=(
                    "Inspect task.json and task directory before trusting or "
                    "retrying this task."
                ),
            )
        )
    if not core.is_supported_schema_version(descriptor.get("schema_version")):
        diagnostics.append(
            diagnostic(
                code="task_schema_unsupported",
                severity="error",
                message=f"task {task_id} has unsupported schema_version",
                suggested_action=(
                    "Upgrade OrchestratorEngine or inspect the task descriptor "
                    f"manually: {descriptor_path}"
                ),
            )
        )
    if descriptor.get("kind") != workers.TASK_KIND:
        diagnostics.append(
            diagnostic(
                code="task_kind_unexpected",
                severity="warning",
                message=f"task {task_id} descriptor kind is not {workers.TASK_KIND}",
                suggested_action="Inspect task.json before trusting this summary.",
            )
        )
    if status not in TASK_STATUSES:
        diagnostics.append(
            diagnostic(
                code="task_status_unknown",
                severity="warning",
                message=f"task {task_id} has unknown status {status!r}",
                suggested_action="Inspect task.json and result.json manually.",
            )
        )
    profile_warnings = descriptor.get("warnings")
    if isinstance(profile_warnings, list):
        for warning in profile_warnings:
            if not isinstance(warning, dict):
                continue
            if warning.get("severity") not in worker_diagnostics.SEVERITIES:
                continue
            diagnostics.append(
                diagnostic(
                    code=str(warning.get("code") or "worker_profile_warning"),
                    severity=str(warning["severity"]),
                    message=str(warning.get("message") or "worker profile warning"),
                    suggested_action=str(
                        warning.get("suggested_action")
                        or "Review the worker profile before accepting this task."
                    ),
                )
            )
    if isinstance(descriptor.get("output_collection_error"), str):
        diagnostics.append(
            diagnostic(
                code="task_declared_output_collection_failed",
                severity="warning",
                message=(
                    f"task {task_id} declared output collection failed: "
                    f"{descriptor['output_collection_error']}"
                ),
                suggested_action=(
                    "Inspect task-local outputs and import the complete result "
                    "before accepting the task."
                ),
            )
        )
    return diagnostics


def running_task_diagnostics(
    *,
    task_id: str,
    status: str,
    supervisor_pid: object,
    supervisor_alive: bool | None,
    worker_pid: object,
    worker_alive: bool | None,
    heartbeat_age: float | None,
    stale_after_seconds: float,
) -> list[dict[str, str]]:
    if status not in RUNNING_STATUSES:
        return []
    diagnostics: list[dict[str, str]] = []
    if not isinstance(supervisor_pid, int):
        diagnostics.append(
            diagnostic(
                code="task_running_without_supervisor_pid",
                severity="warning",
                message=f"task {task_id} is {status} without supervisor_pid",
                suggested_action="Inspect supervisor.log and task.json.",
            )
        )
    elif supervisor_alive is False:
        diagnostics.append(
            diagnostic(
                code="task_supervisor_dead",
                severity="error",
                message=(
                    f"task {task_id} is {status} but supervisor pid "
                    f"{supervisor_pid} is not alive"
                ),
                suggested_action=(
                    "Inspect supervisor.log, worker stdout/stderr and rerun or "
                    "mark the task manually if needed."
                ),
            )
        )
    if isinstance(worker_pid, int) and worker_alive is False:
        diagnostics.append(
            diagnostic(
                code="task_worker_dead",
                severity="warning",
                message=(
                    f"task {task_id} is {status} but worker pid "
                    f"{worker_pid} is not alive"
                ),
                suggested_action=(
                    "Wait for the supervisor to finalize the task; if it does "
                    "not, inspect supervisor.log."
                ),
            )
        )
    if heartbeat_age is None:
        diagnostics.append(
            diagnostic(
                code="task_running_without_heartbeat",
                severity="warning",
                message=f"task {task_id} is {status} without a timestamp",
                suggested_action="Inspect task.json and supervisor.log manually.",
            )
        )
    elif heartbeat_age > stale_after_seconds:
        diagnostics.append(
            diagnostic(
                code="task_heartbeat_stale",
                severity="warning",
                message=(
                    f"task {task_id} heartbeat age {heartbeat_age:.1f}s "
                    f"exceeds {stale_after_seconds:.1f}s"
                ),
                suggested_action=(
                    "If the supervisor is alive, wait or inspect logs; if it is "
                    "dead, treat the task as crashed."
                ),
            )
        )
    return diagnostics


def log_size_diagnostics(
    *,
    task_id: str,
    log_sizes: dict[str, int | None],
    large_log_bytes: int,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    large = {
        name: size
        for name, size in log_sizes.items()
        if isinstance(size, int) and size > large_log_bytes
    }
    if not large:
        return diagnostics
    details = ", ".join(f"{name}={size} bytes" for name, size in sorted(large.items()))
    diagnostics.append(
        diagnostic(
            code="task_large_worker_log",
            severity="info",
            message=(
                f"task {task_id} has large worker log artifacts "
                f"above {large_log_bytes} bytes: {details}"
            ),
            suggested_action=(
                "Read result.json/evidence.json first. Inspect targeted log "
                "tails or failed-command logs instead of pasting full logs "
                "into host chats or reports."
            ),
        )
    )
    return diagnostics


def terminal_task_diagnostics(
    *,
    task_id: str,
    status: str,
    task_dir: Path,
    artifacts: dict[str, Path],
    resolution: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if status not in core.TERMINAL_STATUSES:
        return []
    diagnostics: list[dict[str, str]] = []
    if status in UNSUCCESSFUL_TERMINAL_STATUSES:
        if resolution is not None:
            diagnostics.append(
                diagnostic(
                    code="task_terminal_unsuccessful_resolved",
                    severity="info",
                    message=(
                        f"task {task_id} finished with terminal status {status} "
                        f"and is marked {resolution.get('status')}"
                    ),
                    suggested_action=(
                        "Keep the resolution file and durable task artifacts; "
                        "inspect result/evidence only if the resolution changes."
                    ),
                )
            )
        else:
            diagnostics.append(
                diagnostic(
                    code="task_terminal_unsuccessful",
                    severity="warning",
                    message=f"task {task_id} finished with terminal status {status}",
                    suggested_action=(
                        "Read result.json, then worker stdout/stderr as needed."
                    ),
                )
            )
    for artifact_name in ("result", "evidence"):
        path = artifacts[artifact_name]
        if not path.is_file():
            diagnostics.append(
                diagnostic(
                    code=f"task_missing_{artifact_name}",
                    severity="error",
                    message=(
                        f"task {task_id} is terminal but {artifact_name} is missing"
                    ),
                    suggested_action=f"Inspect {task_dir} and recover {path}.",
                )
            )
            continue
        try:
            core.load_object(path)
        except (OSError, core.OrchestratorError) as error:
            diagnostics.append(
                diagnostic(
                    code=f"task_unreadable_{artifact_name}",
                    severity="error",
                    message=(
                        f"task {task_id} terminal {artifact_name} is unreadable: "
                        f"{error}"
                    ),
                    suggested_action=f"Inspect and repair {path}.",
                )
            )
    for artifact_name in ("event", "signal"):
        path = artifacts.get(artifact_name)
        if path is not None and not path.is_file():
            diagnostics.append(
                diagnostic(
                    code=f"task_missing_{artifact_name}",
                    severity="error",
                    message=f"task {task_id} references missing {artifact_name}",
                    suggested_action=(
                        "Inspect durable audit artifacts; do not delete related "
                        "result/evidence files."
                    ),
                )
            )
    return diagnostics
