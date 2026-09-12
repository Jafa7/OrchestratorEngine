"""Transactional multi-chat continuity and obligation coordination."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import binding, core, delivery_preflight, host_capabilities

KIND = "ORCHESTRATOR_CONTINUITY_STATUS"
SCHEMA_VERSION = 1
DATABASE_SCHEMA_VERSION = 3
WORK_MODES = frozenset({"continue", "waiting", "paused", "complete"})
WAIT_MODES = frozenset({"all", "any"})
OBLIGATION_STATUSES = frozenset({"open", "completed", "failed", "cancelled"})
TERMINAL_OBLIGATION_STATUSES = OBLIGATION_STATUSES - {"open"}
RESPONSE_KINDS = frozenset({"receipt", "progress", "terminal"})
REQUEST_STATUSES = frozenset(
    {"delivery_pending", "claimed", "reply_ready", "handled", "closed_no_reply"}
)
ASSIGNMENT_MODES = frozenset({"continue", "waiting", "paused"})
RECOVERY_STATES = frozenset({"armed", "stopped"})
RECOVERY_SCOPES = frozenset({"project", "actor", "work"})
ACTIVATION_ACTIVE = frozenset({"pending", "published"})
SOURCE_KINDS = frozenset({"obligation", "worker", "check", "ci", "pr", "workstream"})
SOURCE_KIND_MAP = {
    "local_check": "check",
    "github_actions": "ci",
    "github_pull_request": "pr",
    "workstream_checkpoint": "workstream",
}
MAX_ROLE_LENGTH = 128
MAX_SUMMARY_LENGTH = 2000
MAX_OBJECTIVE_LENGTH = 4000
MAX_NEXT_ACTION_LENGTH = 4000
MAX_RESULT_REF_LENGTH = 4096
MAX_REFERENCE_COUNT = 64
MAX_REFERENCE_LENGTH = 1024
MAX_CAPABILITY_COUNT = 64
MAX_CAPABILITY_LENGTH = 128
MAX_ENTRY_OBLIGATIONS = 64
MAX_STATUS_OBLIGATIONS = 64
MAX_STATUS_RESULTS = 64
MAX_STATUS_ACTIVATIONS = 128
MAX_NOTE_LENGTH = 8000
MAX_MESSAGE_LENGTH = 12000
MAX_STATUS_REQUESTS = 64
MAX_STATUS_RESPONSES = 128
DEFAULT_RECOVERY_INTERVAL_SECONDS = 60.0
DEFAULT_RECOVERY_MAX_INTERVAL_SECONDS = 3600.0
MAX_RECOVERY_BATCH = 64


class ContinuityError(RuntimeError):
    """The requested continuity state transition is invalid."""


def _id(value: object, *, field: str) -> str:
    try:
        return core.validate_event_id(value)
    except core.OrchestratorError as error:
        raise ContinuityError(f"invalid {field}: {error}") from error


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContinuityError(f"{field} is required")
    result = value.strip()
    if len(result) > limit:
        raise ContinuityError(f"{field} must be at most {limit} characters")
    return result


def _string_list(
    values: list[str] | None,
    *,
    field: str,
    count_limit: int,
    item_limit: int,
) -> list[str]:
    items = values or []
    if len(items) > count_limit:
        raise ContinuityError(f"{field} may contain at most {count_limit} items")
    return [_text(item, field=f"{field} item", limit=item_limit) for item in items]


def _decode(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def continuity_root(
    project_root: Path, *, state_dir: str = core.DEFAULT_STATE_DIR
) -> Path:
    return core.state_root(project_root, state_dir=state_dir) / "continuity"


def database_path(
    project_root: Path, *, state_dir: str = core.DEFAULT_STATE_DIR
) -> Path:
    return continuity_root(project_root, state_dir=state_dir) / "continuity.sqlite3"


@contextmanager
def _database(
    project_root: Path,
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
    write: bool = False,
) -> Iterator[sqlite3.Connection]:
    path = database_path(project_root, state_dir=state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        _initialize(connection)
        connection.commit()
        if write:
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        if write:
            connection.commit()
    except Exception:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


def _initialize(connection: sqlite3.Connection) -> None:
    metadata_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    if metadata_exists is not None:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if version is not None and version[0] not in {"1", "2", "3"}:
            raise ContinuityError("unsupported continuity database schema")
        if version is not None and version[0] == "1":
            _migrate_database_v1(connection)
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
        if version is not None and version[0] == "2":
            _migrate_database_v2(connection)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS actors (
            actor_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            endpoint_json TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            generation INTEGER NOT NULL,
            active INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS works (
            work_id TEXT PRIMARY KEY,
            objective TEXT NOT NULL,
            references_json TEXT NOT NULL,
            owner_actor TEXT NOT NULL REFERENCES actors(actor_id),
            mode TEXT NOT NULL,
            revision INTEGER NOT NULL,
            control_epoch INTEGER NOT NULL,
            summary TEXT NOT NULL,
            next_action TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS obligations (
            obligation_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(work_id),
            requester_actor TEXT NOT NULL REFERENCES actors(actor_id),
            assignee_actor TEXT NOT NULL REFERENCES actors(actor_id),
            resume_actor TEXT NOT NULL REFERENCES actors(actor_id),
            required INTEGER NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            result_ref TEXT,
            result_digest TEXT,
            generation INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            claimed_activation_id TEXT,
            claimed_generation INTEGER,
            reminder_seconds REAL NOT NULL,
            reminder_count INTEGER NOT NULL,
            max_reminders INTEGER NOT NULL,
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS results (
            outcome_id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            status TEXT NOT NULL,
            event_id TEXT,
            digest TEXT NOT NULL,
            data_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE(source_key, digest)
        );
        CREATE TABLE IF NOT EXISTS waits (
            work_id TEXT PRIMARY KEY REFERENCES works(work_id),
            generation INTEGER NOT NULL,
            mode TEXT NOT NULL,
            sources_json TEXT NOT NULL,
            handled_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS handled_results (
            work_id TEXT NOT NULL REFERENCES works(work_id),
            outcome_id TEXT NOT NULL REFERENCES results(outcome_id),
            source_key TEXT NOT NULL,
            actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            handled_at TEXT NOT NULL,
            PRIMARY KEY(work_id, outcome_id)
        );
        CREATE TABLE IF NOT EXISTS result_aliases (
            alias_outcome_id TEXT PRIMARY KEY,
            outcome_id TEXT NOT NULL REFERENCES results(outcome_id)
        );
        CREATE TABLE IF NOT EXISTS requests (
            request_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL,
            work_id TEXT NOT NULL REFERENCES works(work_id),
            sender_actor TEXT NOT NULL REFERENCES actors(actor_id),
            recipient_actor TEXT NOT NULL REFERENCES actors(actor_id),
            return_actor TEXT NOT NULL REFERENCES actors(actor_id),
            requires_reply INTEGER NOT NULL,
            required INTEGER NOT NULL,
            message_json TEXT NOT NULL,
            recipient_route_json TEXT NOT NULL,
            return_route_json TEXT NOT NULL,
            obligation_id TEXT UNIQUE REFERENCES obligations(obligation_id),
            status TEXT NOT NULL,
            request_activation_id TEXT,
            terminal_response_id TEXT,
            reply_activation_id TEXT,
            handled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS responses (
            response_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL REFERENCES requests(request_id),
            actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            kind TEXT NOT NULL,
            terminal_status TEXT,
            content_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assignment_checkpoints (
            obligation_id TEXT PRIMARY KEY REFERENCES obligations(obligation_id),
            assignment_generation INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            mode TEXT NOT NULL,
            summary TEXT NOT NULL,
            next_action TEXT,
            wait_mode TEXT,
            sources_json TEXT NOT NULL,
            handled_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assignment_handled_results (
            obligation_id TEXT NOT NULL REFERENCES obligations(obligation_id),
            outcome_id TEXT NOT NULL REFERENCES results(outcome_id),
            source_key TEXT NOT NULL,
            actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            handled_at TEXT NOT NULL,
            PRIMARY KEY(obligation_id, outcome_id)
        );
        CREATE TABLE IF NOT EXISTS recovery_policies (
            work_id TEXT PRIMARY KEY REFERENCES works(work_id),
            armed INTEGER NOT NULL,
            interval_seconds REAL NOT NULL,
            max_interval_seconds REAL NOT NULL,
            adopted_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recovery_controls (
            scope_kind TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            state TEXT NOT NULL,
            actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(scope_kind, scope_id)
        );
        CREATE TABLE IF NOT EXISTS recovery_incidents (
            incident_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(work_id),
            obligation_id TEXT REFERENCES obligations(obligation_id),
            actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            cause TEXT NOT NULL,
            work_revision INTEGER NOT NULL,
            control_epoch INTEGER NOT NULL,
            assignment_generation INTEGER,
            endpoint_generation INTEGER NOT NULL,
            capability_level TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            next_inspection_at TEXT NOT NULL,
            activation_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activations (
            activation_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(work_id),
            actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            work_revision INTEGER NOT NULL,
            control_epoch INTEGER NOT NULL,
            endpoint_generation INTEGER NOT NULL,
            assignment_generation INTEGER,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            wake_target_json TEXT NOT NULL,
            not_before TEXT,
            created_at TEXT NOT NULL,
            claimed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS outbox (
            activation_id TEXT PRIMARY KEY REFERENCES activations(activation_id),
            event_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS diagnostics (
            diagnostic_id TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            subject TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finding_digest TEXT,
            resolution_json TEXT
        );
        CREATE TABLE IF NOT EXISTS notes (
            work_id TEXT PRIMARY KEY REFERENCES works(work_id),
            revision INTEGER NOT NULL,
            actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            text TEXT NOT NULL,
            references_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS obligations_work_status_idx
            ON obligations(work_id, status, obligation_id);
        CREATE INDEX IF NOT EXISTS activations_work_status_idx
            ON activations(work_id, status, created_at);
        CREATE INDEX IF NOT EXISTS results_source_recorded_idx
            ON results(source_key, recorded_at, outcome_id);
        CREATE INDEX IF NOT EXISTS handled_results_work_source_idx
            ON handled_results(work_id, source_key, outcome_id);
        CREATE INDEX IF NOT EXISTS requests_work_status_idx
            ON requests(work_id, status, request_id);
        CREATE INDEX IF NOT EXISTS assignment_checkpoint_mode_idx
            ON assignment_checkpoints(mode, updated_at, obligation_id);
        CREATE INDEX IF NOT EXISTS assignment_handled_source_idx
            ON assignment_handled_results(obligation_id, source_key, outcome_id);
        CREATE INDEX IF NOT EXISTS recovery_incident_due_idx
            ON recovery_incidents(status, next_inspection_at, incident_id);
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(DATABASE_SCHEMA_VERSION),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES('activation_sequence', '0')"
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) "
        "VALUES('default_recovery_observation', '0')"
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) "
        "VALUES('default_recovery_interval_seconds', ?)",
        (str(DEFAULT_RECOVERY_INTERVAL_SECONDS),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) "
        "VALUES('default_recovery_max_interval_seconds', ?)",
        (str(DEFAULT_RECOVERY_MAX_INTERVAL_SECONDS),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES('recovery_cursor', '')"
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) "
        "VALUES('recovery_work_cursor', '')"
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) "
        "VALUES('recovery_reply_cursor', '')"
    )
    _normalize_result_identities(connection)
    _backfill_handled_results(connection)
    version = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if version is None or version["value"] != str(DATABASE_SCHEMA_VERSION):
        raise ContinuityError("unsupported continuity database schema")


def _migrate_database_v1(connection: sqlite3.Connection) -> None:
    connection.execute("SAVEPOINT continuity_v1_migration")
    try:
        obligation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(obligations)")
        }
        if "claimed_activation_id" not in obligation_columns:
            connection.execute(
                "ALTER TABLE obligations ADD COLUMN claimed_activation_id TEXT"
            )
        if "claimed_generation" not in obligation_columns:
            connection.execute(
                "ALTER TABLE obligations ADD COLUMN claimed_generation INTEGER"
            )
        activation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(activations)")
        }
        if "assignment_generation" not in activation_columns:
            connection.execute(
                "ALTER TABLE activations ADD COLUMN assignment_generation INTEGER"
            )
        diagnostic_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(diagnostics)")
        }
        if diagnostic_columns and "finding_digest" not in diagnostic_columns:
            connection.execute(
                "ALTER TABLE diagnostics ADD COLUMN finding_digest TEXT"
            )
        if diagnostic_columns and "resolution_json" not in diagnostic_columns:
            connection.execute(
                "ALTER TABLE diagnostics ADD COLUMN resolution_json TEXT"
            )
        result_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(results)")
        }
        if result_columns and "outcome_id" not in result_columns:
            connection.execute("ALTER TABLE results RENAME TO results_v1")
            connection.execute(
                """CREATE TABLE results (
                       outcome_id TEXT PRIMARY KEY,
                       source_key TEXT NOT NULL,
                       status TEXT NOT NULL,
                       event_id TEXT,
                       digest TEXT NOT NULL,
                       data_json TEXT NOT NULL,
                       recorded_at TEXT NOT NULL,
                       UNIQUE(source_key, digest)
                   )"""
            )
            for row in connection.execute("SELECT * FROM results_v1").fetchall():
                connection.execute(
                    """INSERT INTO results(
                           outcome_id, source_key, status, event_id, digest,
                           data_json, recorded_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        f"result-{str(row['digest'])[:24]}",
                        row["source_key"],
                        row["status"],
                        row["event_id"],
                        row["digest"],
                        row["data_json"],
                        row["recorded_at"],
                    ),
                )
            connection.execute("DROP TABLE results_v1")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS handled_results (
                   work_id TEXT NOT NULL REFERENCES works(work_id),
                   outcome_id TEXT NOT NULL REFERENCES results(outcome_id),
                   source_key TEXT NOT NULL,
                   actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                   handled_at TEXT NOT NULL,
                   PRIMARY KEY(work_id, outcome_id)
               )"""
        )
        for obligation in connection.execute(
            "SELECT obligation_id, generation, assignee_actor FROM obligations"
        ).fetchall():
            manifest = _json([f"obligation:{obligation['obligation_id']}"])
            connection.execute(
                """UPDATE activations SET assignment_generation=?
                   WHERE actor_id=? AND manifest_json=?
                     AND reason IN ('obligation_assigned','obligation_reminder')""",
                (obligation["generation"], obligation["assignee_actor"], manifest),
            )
            claimed = connection.execute(
                """SELECT activation_id FROM activations
                   WHERE actor_id=? AND manifest_json=?
                     AND reason IN ('obligation_assigned','obligation_reminder')
                     AND status='claimed'
                   ORDER BY claimed_at DESC, created_at DESC LIMIT 1""",
                (obligation["assignee_actor"], manifest),
            ).fetchone()
            if claimed is not None:
                connection.execute(
                    """UPDATE obligations
                       SET claimed_activation_id=?, claimed_generation=?
                       WHERE obligation_id=?""",
                    (
                        claimed["activation_id"],
                        obligation["generation"],
                        obligation["obligation_id"],
                    ),
                )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            ("2",),
        )
        connection.execute("RELEASE SAVEPOINT continuity_v1_migration")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT continuity_v1_migration")
        connection.execute("RELEASE SAVEPOINT continuity_v1_migration")
        raise


def _migrate_database_v2(connection: sqlite3.Connection) -> None:
    connection.execute("SAVEPOINT continuity_v2_migration")
    try:
        statements = (
            """CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                work_id TEXT NOT NULL REFERENCES works(work_id),
                sender_actor TEXT NOT NULL REFERENCES actors(actor_id),
                recipient_actor TEXT NOT NULL REFERENCES actors(actor_id),
                return_actor TEXT NOT NULL REFERENCES actors(actor_id),
                requires_reply INTEGER NOT NULL,
                required INTEGER NOT NULL,
                message_json TEXT NOT NULL,
                recipient_route_json TEXT NOT NULL,
                return_route_json TEXT NOT NULL,
                obligation_id TEXT UNIQUE REFERENCES obligations(obligation_id),
                status TEXT NOT NULL,
                request_activation_id TEXT,
                terminal_response_id TEXT,
                reply_activation_id TEXT,
                handled_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL REFERENCES requests(request_id),
                actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                kind TEXT NOT NULL,
                terminal_status TEXT,
                content_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS assignment_checkpoints (
                obligation_id TEXT PRIMARY KEY REFERENCES obligations(obligation_id),
                assignment_generation INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                mode TEXT NOT NULL,
                summary TEXT NOT NULL,
                next_action TEXT,
                wait_mode TEXT,
                sources_json TEXT NOT NULL,
                handled_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS assignment_handled_results (
                obligation_id TEXT NOT NULL REFERENCES obligations(obligation_id),
                outcome_id TEXT NOT NULL REFERENCES results(outcome_id),
                source_key TEXT NOT NULL,
                actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                handled_at TEXT NOT NULL,
                PRIMARY KEY(obligation_id, outcome_id)
            )""",
            """CREATE TABLE IF NOT EXISTS recovery_policies (
                work_id TEXT PRIMARY KEY REFERENCES works(work_id),
                armed INTEGER NOT NULL,
                interval_seconds REAL NOT NULL,
                max_interval_seconds REAL NOT NULL,
                adopted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS recovery_controls (
                scope_kind TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                state TEXT NOT NULL,
                actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(scope_kind, scope_id)
            )""",
            """CREATE TABLE IF NOT EXISTS recovery_incidents (
                incident_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL REFERENCES works(work_id),
                obligation_id TEXT REFERENCES obligations(obligation_id),
                actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                cause TEXT NOT NULL,
                work_revision INTEGER NOT NULL,
                control_epoch INTEGER NOT NULL,
                assignment_generation INTEGER,
                endpoint_generation INTEGER NOT NULL,
                capability_level TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                next_inspection_at TEXT NOT NULL,
                activation_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS requests_work_status_idx
                ON requests(work_id, status, request_id)""",
            """CREATE INDEX IF NOT EXISTS assignment_checkpoint_mode_idx
                ON assignment_checkpoints(mode, updated_at, obligation_id)""",
            """CREATE INDEX IF NOT EXISTS assignment_handled_source_idx
                ON assignment_handled_results(
                    obligation_id, source_key, outcome_id
                )""",
            """CREATE INDEX IF NOT EXISTS recovery_incident_due_idx
                ON recovery_incidents(status, next_inspection_at, incident_id)""",
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(DATABASE_SCHEMA_VERSION),),
        )
        connection.execute("RELEASE SAVEPOINT continuity_v2_migration")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT continuity_v2_migration")
        connection.execute("RELEASE SAVEPOINT continuity_v2_migration")
        raise


def _backfill_handled_results(connection: sqlite3.Connection) -> None:
    completed = connection.execute(
        "SELECT value FROM metadata WHERE key='handled_results_backfilled_v2'"
    ).fetchone()
    if completed is not None:
        return
    for wait in connection.execute(
        "SELECT work_id, handled_json, updated_at FROM waits"
    ).fetchall():
        work = connection.execute(
            "SELECT owner_actor FROM works WHERE work_id=?", (wait["work_id"],)
        ).fetchone()
        if work is None:
            continue
        normalized: list[str] = []
        for recorded_identity in _decode(wait["handled_json"], []):
            result = connection.execute(
                """SELECT outcome_id, source_key FROM results
                   WHERE outcome_id=? OR source_key=?
                   ORDER BY recorded_at DESC LIMIT 1""",
                (recorded_identity, recorded_identity),
            ).fetchone()
            if result is None:
                continue
            outcome_id = str(result["outcome_id"])
            normalized.append(outcome_id)
            connection.execute(
                """INSERT OR IGNORE INTO handled_results(
                       work_id, outcome_id, source_key, actor_id, handled_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    wait["work_id"],
                    outcome_id,
                    result["source_key"],
                    work["owner_actor"],
                    wait["updated_at"],
                ),
            )
        connection.execute(
            "UPDATE waits SET handled_json=? WHERE work_id=?",
            (_json(sorted(dict.fromkeys(normalized))), wait["work_id"]),
        )
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES('handled_results_backfilled_v2','1')"
    )


def _normalize_result_identities(connection: sqlite3.Connection) -> None:
    """Upgrade legacy result digests without invalidating published identities."""

    completed = connection.execute(
        "SELECT value FROM metadata WHERE key='result_identity_version'"
    ).fetchone()
    if completed is not None and completed["value"] == "2":
        return
    connection.execute("SAVEPOINT continuity_result_identity_v2")
    try:
        rows = connection.execute(
            "SELECT * FROM results ORDER BY recorded_at, outcome_id"
        ).fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            data = _decode(row["data_json"], {})
            digest, _outcome_id_value = _outcome_identity(
                str(row["source_key"]),
                str(row["status"]),
                data,
                event_id=row["event_id"],
            )
            groups.setdefault((str(row["source_key"]), digest), []).append(row)

        aliases: dict[str, str] = {}
        source_targets: dict[str, list[str]] = {}
        for (source_key, digest), candidates in groups.items():
            canonical = candidates[0]
            canonical_id = str(canonical["outcome_id"])
            source_targets.setdefault(source_key, []).append(canonical_id)
            richest = max(candidates, key=lambda row: len(str(row["data_json"])))
            for duplicate in candidates[1:]:
                duplicate_id = str(duplicate["outcome_id"])
                aliases[duplicate_id] = canonical_id
                connection.execute(
                    """INSERT OR REPLACE INTO result_aliases(
                           alias_outcome_id, outcome_id
                       ) VALUES(?,?)""",
                    (duplicate_id, canonical_id),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO handled_results(
                           work_id, outcome_id, source_key, actor_id, handled_at
                       )
                       SELECT work_id, ?, source_key, actor_id, handled_at
                       FROM handled_results WHERE outcome_id=?""",
                    (canonical_id, duplicate_id),
                )
                connection.execute(
                    "DELETE FROM handled_results WHERE outcome_id=?",
                    (duplicate_id,),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO assignment_handled_results(
                           obligation_id, outcome_id, source_key, actor_id, handled_at
                       )
                       SELECT obligation_id, ?, source_key, actor_id, handled_at
                       FROM assignment_handled_results WHERE outcome_id=?""",
                    (canonical_id, duplicate_id),
                )
                connection.execute(
                    "DELETE FROM assignment_handled_results WHERE outcome_id=?",
                    (duplicate_id,),
                )
                connection.execute(
                    "DELETE FROM results WHERE outcome_id=?", (duplicate_id,)
                )
            connection.execute(
                """UPDATE results SET digest=?, status=?,
                       event_id=COALESCE(event_id, ?), data_json=?
                   WHERE outcome_id=?""",
                (
                    digest,
                    richest["status"],
                    richest["event_id"],
                    richest["data_json"],
                    canonical_id,
                ),
            )

        source_aliases = {
            source_key: targets[0]
            for source_key, targets in source_targets.items()
            if len(targets) == 1
        }
        replacements = {**source_aliases, **aliases}
        for wait in connection.execute(
            "SELECT work_id, handled_json FROM waits"
        ).fetchall():
            values = _decode(wait["handled_json"], [])
            normalized = [replacements.get(value, value) for value in values]
            if normalized != values:
                connection.execute(
                    "UPDATE waits SET handled_json=? WHERE work_id=?",
                    (_json(list(dict.fromkeys(normalized))), wait["work_id"]),
                )
        for checkpoint_row in connection.execute(
            "SELECT obligation_id, handled_json FROM assignment_checkpoints"
        ).fetchall():
            values = _decode(checkpoint_row["handled_json"], [])
            normalized = [replacements.get(value, value) for value in values]
            if normalized != values:
                connection.execute(
                    "UPDATE assignment_checkpoints SET handled_json=? "
                    "WHERE obligation_id=?",
                    (
                        _json(list(dict.fromkeys(normalized))),
                        checkpoint_row["obligation_id"],
                    ),
                )
        for activation in connection.execute(
            """SELECT activation_id, manifest_json FROM activations
               WHERE reason IN ('wait_satisfied','assignment_wait_satisfied')"""
        ).fetchall():
            values = _decode(activation["manifest_json"], [])
            normalized = [replacements.get(value, value) for value in values]
            if normalized != values:
                connection.execute(
                    "UPDATE activations SET manifest_json=? WHERE activation_id=?",
                    (
                        _json(list(dict.fromkeys(normalized))),
                        activation["activation_id"],
                    ),
                )
        connection.execute(
            """INSERT INTO metadata(key, value) VALUES('result_identity_version','2')
               ON CONFLICT(key) DO UPDATE SET value=excluded.value"""
        )
        connection.execute("RELEASE SAVEPOINT continuity_result_identity_v2")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT continuity_result_identity_v2")
        connection.execute("RELEASE SAVEPOINT continuity_result_identity_v2")
        raise


def initialize(
    project_root: Path, *, state_dir: str = core.DEFAULT_STATE_DIR
) -> dict[str, Any]:
    with _database(project_root, state_dir=state_dir):
        pass
    return {
        "schema_version": SCHEMA_VERSION,
        "database_schema_version": DATABASE_SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_CONTINUITY_INITIALIZED",
        "database_path": str(database_path(project_root, state_dir=state_dir)),
    }


def register_actor(
    project_root: Path,
    *,
    actor_id: str,
    role: str,
    host: str,
    target_thread_id: str | None = None,
    capabilities: list[str] | None = None,
    completion_delivery_mode: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    actor_id = _id(actor_id, field="actor_id")
    role = _text(role, field="role", limit=MAX_ROLE_LENGTH)
    capabilities = _string_list(
        capabilities,
        field="capabilities",
        count_limit=MAX_CAPABILITY_COUNT,
        item_limit=MAX_CAPABILITY_LENGTH,
    )
    if target_thread_id is not None:
        target_thread_id = _id(target_thread_id, field="target_thread_id")
    endpoint = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": binding.BINDING_KIND,
        "host": host,
        **({"target_thread_id": target_thread_id} if target_thread_id else {}),
    }
    try:
        binding.validate_binding(endpoint)
    except binding.BindingError as error:
        raise ContinuityError(str(error)) from error
    capability = host_capabilities.for_host(host)
    if capability["endpoint_addressability"] != "exact":
        raise ContinuityError(
            f"host {host} does not provide exact multi-chat addressability"
        )
    wake_target = binding.wake_target_from_binding(endpoint)
    try:
        completion_delivery = delivery_preflight.run(
            project_root,
            operation_kind="continuity",
            operation_id=actor_id,
            wake_policy="always",
            wake_target=wake_target,
            mode=completion_delivery_mode,
            state_dir=state_dir,
        )
        delivery_preflight.enforce(completion_delivery)
    except delivery_preflight.DeliveryPreflightError as error:
        raise ContinuityError(str(error)) from error
    now = core.utc_now()
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        existing = connection.execute(
            "SELECT * FROM actors WHERE actor_id=?", (actor_id,)
        ).fetchone()
        generation = 1
        if existing is not None:
            generation = int(existing["generation"])
            if _decode(existing["endpoint_json"], {}) != endpoint:
                generation += 1
                connection.execute(
                    "UPDATE activations SET status='revoked' "
                    "WHERE actor_id=? AND status IN ('pending','published')",
                    (actor_id,),
                )
                connection.execute(
                    "UPDATE recovery_incidents SET status='superseded', updated_at=? "
                    "WHERE actor_id=? AND status NOT IN ('resolved','superseded')",
                    (now, actor_id),
                )
        connection.execute(
            """INSERT INTO actors(
                   actor_id, role, endpoint_json, capabilities_json,
                   generation, active, updated_at
               ) VALUES(?,?,?,?,?,1,?)
               ON CONFLICT(actor_id) DO UPDATE SET
                   role=excluded.role,
                   endpoint_json=excluded.endpoint_json,
                   capabilities_json=excluded.capabilities_json,
                   generation=excluded.generation,
                   active=1,
                   updated_at=excluded.updated_at""",
            (
                actor_id,
                role,
                _json(endpoint),
                _json(capabilities),
                generation,
                now,
            ),
        )
        if existing is not None and generation != int(existing["generation"]):
            for obligation in connection.execute(
                "SELECT * FROM obligations WHERE assignee_actor=? AND status='open'",
                (actor_id,),
            ).fetchall():
                connection.execute(
                    """UPDATE obligations
                       SET generation=generation+1, reminder_count=0,
                           claimed_at=NULL, claimed_activation_id=NULL,
                           claimed_generation=NULL
                       WHERE obligation_id=?""",
                    (obligation["obligation_id"],),
                )
                current = connection.execute(
                    "SELECT * FROM obligations WHERE obligation_id=?",
                    (obligation["obligation_id"],),
                ).fetchone()
                work = _require_work(connection, obligation["work_id"])
                request = connection.execute(
                    "SELECT * FROM requests WHERE obligation_id=?",
                    (obligation["obligation_id"],),
                ).fetchone()
                assignment = connection.execute(
                    "SELECT * FROM assignment_checkpoints WHERE obligation_id=?",
                    (obligation["obligation_id"],),
                ).fetchone()
                stopped = _effective_explicit_stop(
                    connection, work=work, actor_id=actor_id
                )
                if assignment is not None:
                    connection.execute(
                        "UPDATE assignment_checkpoints SET assignment_generation=?, "
                        "updated_at=? WHERE obligation_id=?",
                        (current["generation"], now, obligation["obligation_id"]),
                    )
                if stopped is None:
                    _rearm_obligation_route(
                        connection,
                        work=work,
                        obligation=current,
                        request=request,
                        assignment=assignment,
                    )
            for request in connection.execute(
                "SELECT * FROM requests WHERE return_actor=? AND status='reply_ready'",
                (actor_id,),
            ).fetchall():
                work = _require_work(connection, request["work_id"])
                if _effective_explicit_stop(
                    connection, work=work, actor_id=actor_id
                ) is not None:
                    continue
                reply_activation = _create_activation(
                    connection,
                    work=work,
                    actor_id=actor_id,
                    reason="request_reply",
                    manifest=[
                        f"request:{request['request_id']}",
                        f"response:{request['terminal_response_id']}",
                    ],
                )
                connection.execute(
                    "UPDATE requests SET reply_activation_id=? WHERE request_id=?",
                    (reply_activation, request["request_id"]),
                )
            for work in connection.execute(
                "SELECT * FROM works WHERE owner_actor=? AND mode!='complete'",
                (actor_id,),
            ).fetchall():
                if _effective_explicit_stop(
                    connection, work=work, actor_id=actor_id
                ) is not None:
                    continue
                if work["mode"] == "continue":
                    _create_activation(
                        connection,
                        work=work,
                        actor_id=actor_id,
                        reason="continue_checkpoint",
                        manifest=[],
                    )
                elif work["mode"] == "waiting":
                    _evaluate_wait(connection, work["work_id"])
    _publish_or_raise(project_root, state_dir=state_dir)
    return delivery_preflight.attach(
        actor_status(project_root, actor_id=actor_id, state_dir=state_dir),
        completion_delivery,
    )


def actor_status(
    project_root: Path,
    *,
    actor_id: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    if actor_id is not None:
        actor_id = _id(actor_id, field="actor_id")
    with _database(project_root, state_dir=state_dir) as connection:
        rows = connection.execute(
            "SELECT * FROM actors"
            + (" WHERE actor_id=?" if actor_id else "")
            + " ORDER BY actor_id",
            ((actor_id,) if actor_id else ()),
        ).fetchall()
    actors = [
        {
            "actor_id": row["actor_id"],
            "role": row["role"],
            "endpoint": _decode(row["endpoint_json"], {}),
            "capabilities": _decode(row["capabilities_json"], []),
            "generation": row["generation"],
            "active": bool(row["active"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    return {"schema_version": SCHEMA_VERSION, "kind": KIND, "actors": actors}


def start_work(
    project_root: Path,
    *,
    work_id: str,
    owner_actor: str,
    objective: str,
    references: list[str] | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    work_id = _id(work_id, field="work_id")
    owner_actor = _id(owner_actor, field="owner_actor")
    objective = _text(objective, field="objective", limit=MAX_OBJECTIVE_LENGTH)
    references = _string_list(
        references,
        field="references",
        count_limit=MAX_REFERENCE_COUNT,
        item_limit=MAX_REFERENCE_LENGTH,
    )
    now = core.utc_now()
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        _require_actor(connection, owner_actor)
        try:
            connection.execute(
                """INSERT INTO works
                   VALUES(?,?,?,?,'paused',1,1,'',NULL,?,?,NULL)""",
                (
                    work_id,
                    objective,
                    _json(references),
                    owner_actor,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ContinuityError(f"work already exists: {work_id}") from error
        recovery_enabled = connection.execute(
            "SELECT value FROM metadata WHERE key='default_recovery_observation'"
        ).fetchone()
        if recovery_enabled is not None and recovery_enabled["value"] == "1":
            interval = float(
                connection.execute(
                    "SELECT value FROM metadata "
                    "WHERE key='default_recovery_interval_seconds'"
                ).fetchone()["value"]
            )
            maximum = float(
                connection.execute(
                    "SELECT value FROM metadata "
                    "WHERE key='default_recovery_max_interval_seconds'"
                ).fetchone()["value"]
            )
            connection.execute(
                """INSERT INTO recovery_policies(
                       work_id, armed, interval_seconds, max_interval_seconds,
                       adopted_at, updated_at
                   ) VALUES(?,1,?,?,?,?)""",
                (work_id, interval, maximum, now, now),
            )
    return status(project_root, work_id=work_id, state_dir=state_dir)


def update_note(
    project_root: Path,
    *,
    work_id: str,
    actor_id: str,
    expected_note_revision: int,
    text: str,
    references: list[str] | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    work_id = _id(work_id, field="work_id")
    actor_id = _id(actor_id, field="actor_id")
    text = _text(text, field="note text", limit=MAX_NOTE_LENGTH)
    references = _string_list(
        references,
        field="note references",
        count_limit=MAX_REFERENCE_COUNT,
        item_limit=MAX_REFERENCE_LENGTH,
    )
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        work = _require_work(connection, work_id)
        _assert_mutable_work(work)
        if work["owner_actor"] != actor_id:
            raise ContinuityError("only the current work owner may update its note")
        current = connection.execute(
            "SELECT * FROM notes WHERE work_id=?", (work_id,)
        ).fetchone()
        revision = int(current["revision"]) if current is not None else 0
        if revision != expected_note_revision:
            raise ContinuityError(
                "stale note revision: "
                f"expected {expected_note_revision}, current {revision}"
            )
        connection.execute(
            """INSERT INTO notes(
                   work_id, revision, actor_id, text, references_json, updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(work_id) DO UPDATE SET
                 revision=excluded.revision, actor_id=excluded.actor_id,
                 text=excluded.text, references_json=excluded.references_json,
                 updated_at=excluded.updated_at""",
            (
                work_id,
                revision + 1,
                actor_id,
                text,
                _json(references),
                core.utc_now(),
            ),
        )
        note = connection.execute(
            "SELECT * FROM notes WHERE work_id=?", (work_id,)
        ).fetchone()
    return _note_dict(note)


def resolve_diagnostic(
    project_root: Path,
    *,
    diagnostic_id: str,
    actor_id: str,
    correction: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    diagnostic_id = _id(diagnostic_id, field="diagnostic_id")
    actor_id = _id(actor_id, field="actor_id")
    correction = _text(
        correction, field="diagnostic correction", limit=MAX_SUMMARY_LENGTH
    )
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        _require_actor(connection, actor_id)
        diagnostic = connection.execute(
            "SELECT * FROM diagnostics WHERE diagnostic_id=?", (diagnostic_id,)
        ).fetchone()
        if diagnostic is None:
            raise ContinuityError(f"unknown diagnostic: {diagnostic_id}")
        resolution = {
            "correction": correction,
            "resolved_by": actor_id,
            "resolved_at": core.utc_now(),
        }
        connection.execute(
            """UPDATE diagnostics
               SET status='resolved', resolution_json=?, updated_at=?
               WHERE diagnostic_id=?""",
            (_json(resolution), core.utc_now(), diagnostic_id),
        )
    return {
        "diagnostic_id": diagnostic_id,
        "status": "resolved",
        "resolved_by": actor_id,
    }


def open_obligation(
    project_root: Path,
    *,
    work_id: str,
    obligation_id: str,
    requester_actor: str,
    assignee_actor: str,
    resume_actor: str,
    summary: str,
    required: bool = True,
    reminder_seconds: float = 600.0,
    max_reminders: int = 2,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    work_id = _id(work_id, field="work_id")
    obligation_id = _id(obligation_id, field="obligation_id")
    requester_actor = _id(requester_actor, field="requester_actor")
    assignee_actor = _id(assignee_actor, field="assignee_actor")
    resume_actor = _id(resume_actor, field="resume_actor")
    summary = _text(summary, field="summary", limit=MAX_SUMMARY_LENGTH)
    if reminder_seconds < 10 or reminder_seconds > 86400:
        raise ContinuityError("reminder_seconds must be between 10 and 86400")
    if max_reminders < 0 or max_reminders > 10:
        raise ContinuityError("max_reminders must be between 0 and 10")
    now = core.utc_now()
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        work = _require_work(connection, work_id)
        _assert_mutable_work(work)
        for actor_id in (requester_actor, assignee_actor, resume_actor):
            _require_actor(connection, actor_id)
        if work["owner_actor"] != requester_actor:
            raise ContinuityError("only the current work owner may open an obligation")
        if resume_actor != work["owner_actor"]:
            raise ContinuityError("resume actor must be the current work owner")
        try:
            connection.execute(
                """INSERT INTO obligations(
                       obligation_id, work_id, requester_actor, assignee_actor,
                       resume_actor, required, status, summary, result_ref,
                       result_digest, generation, created_at, claimed_at,
                       claimed_activation_id, claimed_generation,
                       reminder_seconds, reminder_count, max_reminders, resolved_at
                   ) VALUES(
                       ?,?,?,?,?,?,'open',?,NULL,NULL,1,?,NULL,NULL,NULL,?,0,?,NULL
                   )""",
                (
                    obligation_id,
                    work_id,
                    requester_actor,
                    assignee_actor,
                    resume_actor,
                    int(required),
                    summary,
                    now,
                    reminder_seconds,
                    max_reminders,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ContinuityError(
                f"obligation already exists: {obligation_id}"
            ) from error
        obligation = connection.execute(
            "SELECT * FROM obligations WHERE obligation_id=?", (obligation_id,)
        ).fetchone()
        activation = _schedule_obligation_activations(
            connection,
            work=work,
            actor_id=assignee_actor,
            obligation=obligation,
        )
    _publish_or_raise(project_root, state_dir=state_dir)
    return {"obligation_id": obligation_id, "activation_id": activation}


def request_send(
    project_root: Path,
    *,
    work_id: str,
    request_id: str,
    idempotency_key: str,
    sender_actor: str,
    recipient_actor: str,
    return_actor: str,
    message: str | None = None,
    message_ref: str | None = None,
    requires_reply: bool = False,
    required: bool = False,
    reminder_seconds: float = 600.0,
    max_reminders: int = 2,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Persist a routed request, optional reply obligation and outbox atomically."""

    work_id = _id(work_id, field="work_id")
    request_id = _id(request_id, field="request_id")
    idempotency_key = _id(idempotency_key, field="idempotency_key")
    sender_actor = _id(sender_actor, field="sender_actor")
    recipient_actor = _id(recipient_actor, field="recipient_actor")
    return_actor = _id(return_actor, field="return_actor")
    if required and not requires_reply:
        raise ContinuityError("required is valid only when requires_reply is true")
    if reminder_seconds < 10 or reminder_seconds > 86400:
        raise ContinuityError("reminder_seconds must be between 10 and 86400")
    if max_reminders < 0 or max_reminders > 10:
        raise ContinuityError("max_reminders must be between 0 and 10")
    message_value = _message_value(
        project_root, message=message, message_ref=message_ref
    )
    now = core.utc_now()
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        work = _require_work(connection, work_id)
        _assert_mutable_work(work)
        sender = _require_actor(connection, sender_actor)
        recipient = _require_actor(connection, recipient_actor)
        return_row = _require_actor(connection, return_actor)
        recipient_route = _actor_route(recipient)
        return_route = _actor_route(return_row)
        existing = connection.execute(
            "SELECT * FROM requests WHERE idempotency_key=? OR request_id=?",
            (idempotency_key, request_id),
        ).fetchone()
        fingerprint_recipient_route = (
            _decode(existing["recipient_route_json"], {})
            if existing is not None
            else recipient_route
        )
        fingerprint_return_route = (
            _decode(existing["return_route_json"], {})
            if existing is not None
            else return_route
        )
        fingerprint = _digest(
            {
                "work_id": work_id,
                "request_id": request_id,
                "sender_actor": sender_actor,
                "recipient_actor": recipient_actor,
                "return_actor": return_actor,
                "message": message_value,
                "requires_reply": requires_reply,
                "required": required,
                "recipient_route": fingerprint_recipient_route,
                "return_route": fingerprint_return_route,
            }
        )
        if existing is not None:
            if (
                existing["idempotency_key"] != idempotency_key
                or existing["request_id"] != request_id
                or existing["fingerprint"] != fingerprint
            ):
                raise ContinuityError("request idempotency identity conflicts")
            response = _request_dict(existing)
            response["idempotent"] = True
        else:
            del sender
            obligation_id = None
            if requires_reply:
                obligation_id = f"reply-{_digest({'request_id': request_id})[:24]}"
                connection.execute(
                    """INSERT INTO obligations(
                           obligation_id, work_id, requester_actor, assignee_actor,
                           resume_actor, required, status, summary, result_ref,
                           result_digest, generation, created_at, claimed_at,
                           claimed_activation_id, claimed_generation,
                           reminder_seconds, reminder_count, max_reminders, resolved_at
                       ) VALUES(
                           ?,?,?,?,?,?,'open',?,NULL,NULL,1,?,NULL,NULL,NULL,?,0,?,NULL
                       )""",
                    (
                        obligation_id,
                        work_id,
                        sender_actor,
                        recipient_actor,
                        return_actor,
                        int(required),
                        f"Reply to request {request_id}",
                        now,
                        reminder_seconds,
                        max_reminders,
                    ),
                )
            connection.execute(
                """INSERT INTO requests(
                       request_id, idempotency_key, fingerprint, work_id,
                       sender_actor, recipient_actor, return_actor, requires_reply,
                       required, message_json, recipient_route_json,
                       return_route_json, obligation_id, status,
                       request_activation_id, terminal_response_id,
                       reply_activation_id, handled_at, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'delivery_pending',
                            NULL,NULL,NULL,NULL,?,?)""",
                (
                    request_id,
                    idempotency_key,
                    fingerprint,
                    work_id,
                    sender_actor,
                    recipient_actor,
                    return_actor,
                    int(requires_reply),
                    int(required),
                    _json(message_value),
                    _json(recipient_route),
                    _json(return_route),
                    obligation_id,
                    now,
                    now,
                ),
            )
            manifest = [f"request:{request_id}"]
            assignment_generation = None
            if obligation_id is not None:
                manifest.append(f"obligation:{obligation_id}")
                assignment_generation = 1
            activation_id = None
            if _effective_explicit_stop(
                connection, work=work, actor_id=recipient_actor
            ) is None:
                activation_id = _create_activation(
                    connection,
                    work=work,
                    actor_id=recipient_actor,
                    reason="request_message",
                    manifest=manifest,
                    assignment_generation=assignment_generation,
                )
                connection.execute(
                    "UPDATE requests SET request_activation_id=? WHERE request_id=?",
                    (activation_id, request_id),
                )
            if obligation_id is not None and activation_id is not None:
                scheduled_at = datetime.now(UTC)
                for index in range(max_reminders):
                    _create_activation(
                        connection,
                        work=work,
                        actor_id=recipient_actor,
                        reason="obligation_reminder",
                        manifest=manifest,
                        assignment_generation=1,
                        not_before=(
                            scheduled_at
                            + timedelta(seconds=reminder_seconds * (index + 1))
                        ).isoformat(timespec="milliseconds"),
                    )
                connection.execute(
                    "UPDATE obligations SET reminder_count=? WHERE obligation_id=?",
                    (max_reminders, obligation_id),
                )
            created = connection.execute(
                "SELECT * FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
            response = _request_dict(created)
            response["idempotent"] = False
    publication = _publish_or_raise(project_root, state_dir=state_dir)
    return {**response, **publication}


def request_respond(
    project_root: Path,
    *,
    request_id: str,
    response_id: str,
    actor_id: str,
    activation_id: str,
    kind: str,
    content: str | None = None,
    content_ref: str | None = None,
    terminal_status: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Record request progress or atomically save one terminal reply intent."""

    request_id = _id(request_id, field="request_id")
    response_id = _id(response_id, field="response_id")
    actor_id = _id(actor_id, field="actor_id")
    activation_id = _id(activation_id, field="activation_id")
    if kind not in RESPONSE_KINDS:
        raise ContinuityError(f"unsupported response kind: {kind}")
    if kind == "terminal":
        if terminal_status not in TERMINAL_OBLIGATION_STATUSES:
            raise ContinuityError(
                "terminal response requires completed, failed or cancelled status"
            )
    elif terminal_status is not None:
        raise ContinuityError("terminal_status is valid only for terminal responses")
    content_value = _message_value(
        project_root, message=content, message_ref=content_ref
    )
    fingerprint = _digest(
        {
            "request_id": request_id,
            "response_id": response_id,
            "actor_id": actor_id,
            "kind": kind,
            "terminal_status": terminal_status,
            "content": content_value,
        }
    )
    now = core.utc_now()
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        request = connection.execute(
            "SELECT * FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if request is None:
            raise ContinuityError(f"unknown request: {request_id}")
        existing = connection.execute(
            "SELECT * FROM responses WHERE response_id=?", (response_id,)
        ).fetchone()
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise ContinuityError("response identity conflicts")
            response = _response_dict(existing)
            response["idempotent"] = True
        else:
            if not bool(request["requires_reply"]):
                raise ContinuityError("informational request does not accept responses")
            if request["recipient_actor"] != actor_id:
                raise ContinuityError("only the request recipient may respond")
            if request["terminal_response_id"] is not None:
                raise ContinuityError("request already has a terminal response")
            obligation = connection.execute(
                "SELECT * FROM obligations WHERE obligation_id=?",
                (request["obligation_id"],),
            ).fetchone()
            if obligation is None or obligation["status"] != "open":
                raise ContinuityError("reply obligation is no longer open")
            _assert_current_assignment_claim(
                connection,
                obligation=obligation,
                actor_id=actor_id,
                activation_id=activation_id,
            )
            connection.execute(
                """INSERT INTO responses(
                       response_id, request_id, actor_id, kind, terminal_status,
                       content_json, fingerprint, created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    response_id,
                    request_id,
                    actor_id,
                    kind,
                    terminal_status,
                    _json(content_value),
                    fingerprint,
                    now,
                ),
            )
            if kind == "terminal":
                reference = _message_reference(content_value)
                connection.execute(
                    """UPDATE obligations
                       SET status=?, result_ref=?, result_digest=?, resolved_at=?
                       WHERE obligation_id=?""",
                    (
                        terminal_status,
                        reference["reference"],
                        reference["digest"],
                        now,
                        obligation["obligation_id"],
                    ),
                )
                _revoke_request_activations(
                    connection,
                    request_id=request_id,
                    actor_id=actor_id,
                )
                _changed, outcome_id = _record_result(
                    connection,
                    source_key=f"obligation:{obligation['obligation_id']}",
                    status_value=str(terminal_status),
                    event_id=None,
                    data={
                        "request_id": request_id,
                        "response_id": response_id,
                        "response": content_value,
                        "resume_actor": request["return_actor"],
                        "assignment_activation_id": activation_id,
                        "assignment_generation": obligation["generation"],
                    },
                )
                work = _require_work(connection, request["work_id"])
                reply_activation = None
                if _effective_explicit_stop(
                    connection, work=work, actor_id=request["return_actor"]
                ) is None:
                    reply_activation = _create_activation(
                        connection,
                        work=work,
                        actor_id=request["return_actor"],
                        reason="request_reply",
                        manifest=[
                            f"request:{request_id}",
                            f"response:{response_id}",
                            outcome_id,
                        ],
                    )
                connection.execute(
                    """UPDATE requests
                       SET status='reply_ready', terminal_response_id=?,
                           reply_activation_id=?, updated_at=?
                       WHERE request_id=?""",
                    (response_id, reply_activation, now, request_id),
                )
                connection.execute(
                    """UPDATE recovery_incidents SET status='resolved', updated_at=?
                       WHERE obligation_id=? AND status!='resolved'""",
                    (now, obligation["obligation_id"]),
                )
                _evaluate_wait(connection, request["work_id"])
            else:
                connection.execute(
                    "UPDATE requests SET status='claimed', updated_at=? "
                    "WHERE request_id=?",
                    (now, request_id),
                )
            created = connection.execute(
                "SELECT * FROM responses WHERE response_id=?", (response_id,)
            ).fetchone()
            response = _response_dict(created)
            response["idempotent"] = False
    publication = _publish_or_raise(project_root, state_dir=state_dir)
    return {**response, **publication}


def request_handle(
    project_root: Path,
    *,
    request_id: str,
    actor_id: str,
    activation_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Acknowledge handling of a saved terminal reply without accepting product work."""

    request_id = _id(request_id, field="request_id")
    actor_id = _id(actor_id, field="actor_id")
    activation_id = _id(activation_id, field="activation_id")
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        request = connection.execute(
            "SELECT * FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if request is None:
            raise ContinuityError(f"unknown request: {request_id}")
        if request["return_actor"] != actor_id:
            raise ContinuityError("only the pinned return actor may handle the reply")
        if request["status"] == "handled":
            return {"request_id": request_id, "status": "handled", "idempotent": True}
        if request["status"] != "reply_ready":
            raise ContinuityError("terminal reply is not ready for handling")
        activation = connection.execute(
            "SELECT * FROM activations WHERE activation_id=?", (activation_id,)
        ).fetchone()
        actor = _require_actor(connection, actor_id)
        if (
            activation is None
            or activation["status"] != "claimed"
            or activation["actor_id"] != actor_id
            or activation["reason"] != "request_reply"
            or request["reply_activation_id"] != activation_id
            or int(activation["endpoint_generation"]) != int(actor["generation"])
            or f"request:{request_id}"
            not in _decode(activation["manifest_json"], [])
        ):
            raise ContinuityError(
                "request reply requires its current claimed activation"
            )
        connection.execute(
            "UPDATE requests SET status='handled', handled_at=?, updated_at=? "
            "WHERE request_id=?",
            (core.utc_now(), core.utc_now(), request_id),
        )
        _revoke_manifest_activations(
            connection,
            marker=f"request:{request_id}",
            reasons={"work_recovery"},
        )
        _resolve_recovery_incidents_for_manifest(
            connection,
            marker=f"request:{request_id}",
            cause="unconfirmed_reply_handling",
        )
    return {"request_id": request_id, "status": "handled", "idempotent": False}


def assignment_checkpoint(
    project_root: Path,
    *,
    obligation_id: str,
    actor_id: str,
    activation_id: str,
    expected_revision: int,
    mode: str,
    summary: str,
    next_action: str | None = None,
    wait_mode: str = "all",
    wait_on: list[str] | None = None,
    handled_results: list[str] | None = None,
    reprocess_results: list[str] | None = None,
    delay_seconds: float = 10.0,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Checkpoint only one claimed assignment, without changing work ownership."""

    obligation_id = _id(obligation_id, field="obligation_id")
    actor_id = _id(actor_id, field="actor_id")
    activation_id = _id(activation_id, field="activation_id")
    summary = _text(summary, field="summary", limit=MAX_SUMMARY_LENGTH)
    if mode not in ASSIGNMENT_MODES:
        raise ContinuityError(f"unsupported assignment mode: {mode}")
    if wait_mode not in WAIT_MODES:
        raise ContinuityError(f"unsupported wait mode: {wait_mode}")
    if next_action is not None:
        next_action = _text(
            next_action, field="next_action", limit=MAX_NEXT_ACTION_LENGTH
        )
    sources = [_source_key(value) for value in (wait_on or [])]
    handled_results = [
        _id(value, field="handled result") for value in (handled_results or [])
    ]
    reprocess_results = [
        _id(value, field="reprocess result") for value in (reprocess_results or [])
    ]
    if mode == "waiting" and not sources:
        raise ContinuityError("waiting assignment requires a typed wait source")
    if mode != "waiting" and sources:
        raise ContinuityError("wait sources are valid only in waiting mode")
    if mode != "waiting" and reprocess_results:
        raise ContinuityError("reprocessed results are valid only in waiting mode")
    if mode == "continue" and not next_action:
        raise ContinuityError("continue assignment requires next_action")
    if delay_seconds < 0 or delay_seconds > 3600:
        raise ContinuityError("delay_seconds must be between 0 and 3600")
    now = core.utc_now()
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        obligation = connection.execute(
            "SELECT * FROM obligations WHERE obligation_id=?", (obligation_id,)
        ).fetchone()
        if obligation is None or obligation["status"] != "open":
            raise ContinuityError("assignment is no longer open")
        _assert_current_assignment_claim(
            connection,
            obligation=obligation,
            actor_id=actor_id,
            activation_id=activation_id,
        )
        previous = connection.execute(
            "SELECT * FROM assignment_checkpoints WHERE obligation_id=?",
            (obligation_id,),
        ).fetchone()
        revision = int(previous["revision"]) if previous is not None else 0
        if revision != expected_revision:
            raise ContinuityError(
                "stale assignment revision: "
                f"expected {expected_revision}, current {revision}"
            )
        previous_sources = (
            _decode(previous["sources_json"], [])
            if previous is not None and previous["mode"] == "waiting"
            else []
        )
        ledger_sources = sources if mode == "waiting" else previous_sources
        acknowledged = _next_assignment_handled_results(
            connection,
            obligation_id=obligation_id,
            actor_id=actor_id,
            sources=ledger_sources,
            handled_results=handled_results,
            reprocess_results=reprocess_results,
        )
        if mode == "paused":
            generation = int(obligation["generation"])
            _revoke_obligation_activations(
                connection, obligation_id=obligation_id
            )
        else:
            generation = int(obligation["generation"]) + 1
            connection.execute(
                """UPDATE obligations
                   SET generation=?, claimed_at=NULL, claimed_activation_id=NULL,
                       claimed_generation=NULL WHERE obligation_id=?""",
                (generation, obligation_id),
            )
            _revoke_obligation_activations(
                connection, obligation_id=obligation_id
            )
        connection.execute(
            """INSERT INTO assignment_checkpoints(
                   obligation_id, assignment_generation, revision, mode, summary,
                   next_action, wait_mode, sources_json, handled_json, updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(obligation_id) DO UPDATE SET
                 assignment_generation=excluded.assignment_generation,
                 revision=excluded.revision, mode=excluded.mode,
                 summary=excluded.summary, next_action=excluded.next_action,
                 wait_mode=excluded.wait_mode, sources_json=excluded.sources_json,
                 handled_json=excluded.handled_json, updated_at=excluded.updated_at""",
            (
                obligation_id,
                generation,
                revision + 1,
                mode,
                summary,
                next_action,
                wait_mode if mode == "waiting" else None,
                _json(sources),
                _json(sorted(acknowledged)),
                now,
            ),
        )
        connection.execute(
            """UPDATE recovery_incidents SET status='resolved', updated_at=?
               WHERE obligation_id=? AND status!='resolved'""",
            (now, obligation_id),
        )
        work = _require_work(connection, obligation["work_id"])
        stopped = _effective_explicit_stop(
            connection, work=work, actor_id=actor_id
        )
        next_activation = None
        if mode == "continue" and stopped is None:
            next_activation = _create_activation(
                connection,
                work=work,
                actor_id=actor_id,
                reason="assignment_continue",
                manifest=[f"obligation:{obligation_id}"],
                assignment_generation=generation,
                not_before=(
                    datetime.now(UTC) + timedelta(seconds=delay_seconds)
                ).isoformat(timespec="milliseconds"),
            )
        elif mode == "waiting":
            _reconcile_sources(
                project_root,
                connection,
                sources=sources,
                state_dir=state_dir,
            )
            if stopped is None:
                next_activation = _evaluate_assignment_wait(
                    connection, obligation_id
                )
    publication = _publish_or_raise(project_root, state_dir=state_dir)
    return {
        "obligation_id": obligation_id,
        "assignment_revision": revision + 1,
        "assignment_generation": generation,
        "mode": mode,
        "activation_id": next_activation,
        **publication,
    }


def configure_recovery(
    project_root: Path,
    *,
    enabled: bool,
    interval_seconds: float = DEFAULT_RECOVERY_INTERVAL_SECONDS,
    max_interval_seconds: float = DEFAULT_RECOVERY_MAX_INTERVAL_SECONDS,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    _validate_recovery_intervals(interval_seconds, max_interval_seconds)
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) "
            "VALUES('default_recovery_observation', ?)",
            ("1" if enabled else "0",),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) "
            "VALUES('default_recovery_interval_seconds', ?)",
            (str(interval_seconds),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) "
            "VALUES('default_recovery_max_interval_seconds', ?)",
            (str(max_interval_seconds),),
        )
    return {
        "enabled": enabled,
        "interval_seconds": interval_seconds,
        "max_interval_seconds": max_interval_seconds,
        "historical_work_armed": False,
    }


def adopt_recovery(
    project_root: Path,
    *,
    work_ids: list[str],
    actor_id: str,
    interval_seconds: float | None = None,
    max_interval_seconds: float | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    actor_id = _id(actor_id, field="actor_id")
    work_ids = [_id(value, field="work_id") for value in work_ids]
    if not work_ids:
        raise ContinuityError("at least one work_id is required")
    now = core.utc_now()
    adopted: list[str] = []
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        _require_actor(connection, actor_id)
        default_interval = float(
            connection.execute(
                "SELECT value FROM metadata "
                "WHERE key='default_recovery_interval_seconds'"
            ).fetchone()["value"]
        )
        default_maximum = float(
            connection.execute(
                "SELECT value FROM metadata "
                "WHERE key='default_recovery_max_interval_seconds'"
            ).fetchone()["value"]
        )
        interval = (
            interval_seconds if interval_seconds is not None else default_interval
        )
        maximum = (
            max_interval_seconds
            if max_interval_seconds is not None
            else default_maximum
        )
        _validate_recovery_intervals(interval, maximum)
        for work_id in work_ids:
            work = _require_work(connection, work_id)
            _assert_mutable_work(work)
            if work["owner_actor"] != actor_id:
                raise ContinuityError("only the work owner may adopt recovery")
            connection.execute(
                """INSERT INTO recovery_policies(
                       work_id, armed, interval_seconds, max_interval_seconds,
                       adopted_at, updated_at
                   ) VALUES(?,1,?,?,?,?)
                   ON CONFLICT(work_id) DO UPDATE SET
                     armed=1, interval_seconds=excluded.interval_seconds,
                     max_interval_seconds=excluded.max_interval_seconds,
                     updated_at=excluded.updated_at""",
                (work_id, interval, maximum, now, now),
            )
            adopted.append(work_id)
    return {"adopted": adopted, "armed": True}


def set_recovery_control(
    project_root: Path,
    *,
    scope_kind: str,
    scope_id: str,
    state: str,
    actor_id: str,
    reason: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    if scope_kind not in RECOVERY_SCOPES:
        raise ContinuityError(f"unsupported recovery scope: {scope_kind}")
    if state not in RECOVERY_STATES:
        raise ContinuityError(f"unsupported recovery state: {state}")
    scope_id = _id(scope_id, field="scope_id")
    actor_id = _id(actor_id, field="actor_id")
    reason = _text(reason, field="reason", limit=MAX_SUMMARY_LENGTH)
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        actor = _require_actor(connection, actor_id)
        if scope_kind == "work":
            work = _require_work(connection, scope_id)
            if work["owner_actor"] != actor_id:
                raise ContinuityError("only the work owner may change work recovery")
        elif scope_kind == "actor" and scope_id != actor_id:
            raise ContinuityError("an actor may change only its own recovery scope")
        elif scope_kind == "project" and "project_control" not in _decode(
            actor["capabilities_json"], []
        ):
            raise ContinuityError("project recovery control requires project_control")
        connection.execute(
            """INSERT INTO recovery_controls(
                   scope_kind, scope_id, state, actor_id, reason, updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(scope_kind, scope_id) DO UPDATE SET
                 state=excluded.state, actor_id=excluded.actor_id,
                 reason=excluded.reason, updated_at=excluded.updated_at""",
            (scope_kind, scope_id, state, actor_id, reason, core.utc_now()),
        )
        if state == "stopped":
            for activation in connection.execute(
                """SELECT activation_id, work_id, actor_id FROM activations
                   WHERE status IN ('pending','published')"""
            ).fetchall():
                covered = (
                    scope_kind == "project"
                    or (scope_kind == "actor" and activation["actor_id"] == scope_id)
                    or (scope_kind == "work" and activation["work_id"] == scope_id)
                )
                if not covered:
                    continue
                connection.execute(
                    "UPDATE activations SET status='revoked' WHERE activation_id=?",
                    (activation["activation_id"],),
                )
                connection.execute(
                    "UPDATE recovery_incidents SET status='stopped', updated_at=? "
                    "WHERE activation_id=?",
                    (core.utc_now(), activation["activation_id"]),
                )
        else:
            _rearm_scope_routes(
                connection, scope_kind=scope_kind, scope_id=scope_id
            )
    publication = _publish_or_raise(project_root, state_dir=state_dir)
    return {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "state": state,
        "actor_id": actor_id,
        "reason": reason,
        **publication,
    }


def resolve_obligation(
    project_root: Path,
    *,
    obligation_id: str,
    actor_id: str,
    activation_id: str,
    status_value: str,
    result_ref: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    obligation_id = _id(obligation_id, field="obligation_id")
    actor_id = _id(actor_id, field="actor_id")
    activation_id = _id(activation_id, field="activation_id")
    result_ref = _text(result_ref, field="result_ref", limit=MAX_RESULT_REF_LENGTH)
    if status_value not in TERMINAL_OBLIGATION_STATUSES:
        raise ContinuityError(
            "obligation result must be completed, failed or cancelled"
        )
    now = core.utc_now()
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        obligation = connection.execute(
            "SELECT * FROM obligations WHERE obligation_id=?", (obligation_id,)
        ).fetchone()
        if obligation is None:
            raise ContinuityError(f"unknown obligation: {obligation_id}")
        linked_request = connection.execute(
            "SELECT request_id FROM requests WHERE obligation_id=?",
            (obligation_id,),
        ).fetchone()
        if linked_request is not None:
            raise ContinuityError(
                "managed reply obligations must be resolved with request-respond"
            )
        work = _require_work(connection, obligation["work_id"])
        _assert_mutable_work(work)
        if obligation["assignee_actor"] != actor_id:
            raise ContinuityError("only the assigned actor may resolve the obligation")
        idempotent = obligation["status"] != "open"
        if idempotent:
            if (
                obligation["status"] == status_value
                and obligation["result_ref"] == result_ref
                and obligation["claimed_activation_id"] == activation_id
            ):
                pass
            else:
                raise ContinuityError("obligation is already terminal")
        else:
            if (
                obligation["claimed_activation_id"] != activation_id
                or obligation["claimed_generation"] != obligation["generation"]
            ):
                raise ContinuityError(
                    "obligation resolution requires the current claimed assignment"
                )
            activation = connection.execute(
                "SELECT * FROM activations WHERE activation_id=?",
                (activation_id,),
            ).fetchone()
            if (
                activation is None
                or activation["status"] != "claimed"
                or activation["actor_id"] != actor_id
                or activation["endpoint_generation"]
                != _require_actor(connection, actor_id)["generation"]
            ):
                raise ContinuityError("assignment claim is stale or unavailable")
            reference = _result_reference(project_root, result_ref)
            connection.execute(
                """UPDATE obligations
                   SET status=?, result_ref=?, result_digest=?, resolved_at=?
                   WHERE obligation_id=?""",
                (
                    status_value,
                    result_ref,
                    reference.get("content_sha256"),
                    now,
                    obligation_id,
                ),
            )
            _revoke_obligation_activations(
                connection, obligation_id=obligation_id
            )
            _record_result(
                connection,
                source_key=f"obligation:{obligation_id}",
                status_value=status_value,
                event_id=None,
                data={
                    "result_reference": reference,
                    "resume_actor": obligation["resume_actor"],
                    "assignment_activation_id": activation_id,
                    "assignment_generation": obligation["generation"],
                },
            )
            connection.execute(
                """UPDATE recovery_incidents SET status='resolved', updated_at=?
                   WHERE obligation_id=? AND status!='resolved'""",
                (now, obligation_id),
            )
            _evaluate_wait(connection, obligation["work_id"])
    _publish_or_raise(project_root, state_dir=state_dir)
    return {
        "obligation_id": obligation_id,
        "activation_id": activation_id,
        "status": status_value,
        "idempotent": idempotent,
    }


def checkpoint(
    project_root: Path,
    *,
    work_id: str,
    actor_id: str,
    expected_revision: int,
    mode: str,
    summary: str,
    next_action: str | None = None,
    wait_mode: str = "all",
    wait_on: list[str] | None = None,
    handled_results: list[str] | None = None,
    reprocess_results: list[str] | None = None,
    delay_seconds: float = 10.0,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    work_id = _id(work_id, field="work_id")
    actor_id = _id(actor_id, field="actor_id")
    summary = _text(summary, field="summary", limit=MAX_SUMMARY_LENGTH)
    if next_action is not None:
        next_action = _text(
            next_action, field="next_action", limit=MAX_NEXT_ACTION_LENGTH
        )
    if mode not in WORK_MODES:
        raise ContinuityError(f"unsupported work mode: {mode}")
    if wait_mode not in WAIT_MODES:
        raise ContinuityError(f"unsupported wait mode: {wait_mode}")
    sources = [_source_key(value) for value in (wait_on or [])]
    handled_results = [
        _id(value, field="handled result") for value in (handled_results or [])
    ]
    reprocess_results = [
        _id(value, field="reprocess result") for value in (reprocess_results or [])
    ]
    if mode == "waiting" and not sources:
        raise ContinuityError("waiting mode requires at least one typed wait source")
    if mode != "waiting" and sources:
        raise ContinuityError("wait sources are valid only in waiting mode")
    if mode != "waiting" and reprocess_results:
        raise ContinuityError("reprocessed results are valid only in waiting mode")
    if mode == "continue" and not next_action:
        raise ContinuityError("continue mode requires next_action")
    if delay_seconds < 0 or delay_seconds > 3600:
        raise ContinuityError("delay_seconds must be between 0 and 3600")
    now = core.utc_now()
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        work = _require_work(connection, work_id)
        _assert_owner_revision(
            work, actor_id=actor_id, expected_revision=expected_revision
        )
        _assert_mutable_work(work)
        if mode == "complete":
            open_required = connection.execute(
                """SELECT obligation_id FROM obligations
                   WHERE work_id=? AND required=1 AND status='open'""",
                (work_id,),
            ).fetchall()
            if open_required:
                ids = ", ".join(row["obligation_id"] for row in open_required)
                raise ContinuityError(f"required obligations remain open: {ids}")
        revision = int(work["revision"]) + 1
        connection.execute(
            """UPDATE works SET mode=?, revision=?, summary=?, next_action=?,
                   control_epoch=control_epoch+1, updated_at=?, completed_at=?
               WHERE work_id=?""",
            (
                mode,
                revision,
                summary,
                next_action,
                now,
                now if mode == "complete" else None,
                work_id,
            ),
        )
        connection.execute(
            """UPDATE recovery_incidents SET status='resolved', updated_at=?
               WHERE work_id=? AND obligation_id IS NULL
                 AND cause='unconfirmed_owner_checkpoint'
                 AND status!='resolved'""",
            (now, work_id),
        )
        if mode == "complete":
            _revoke_product_activations_on_complete(
                connection, work_id=work_id
            )
        else:
            _revoke_owner_control_activations(
                connection, work_id=work_id, actor_id=actor_id
            )
        activation = None
        updated = _require_work(connection, work_id)
        if mode == "waiting":
            previous = connection.execute(
                "SELECT * FROM waits WHERE work_id=?", (work_id,)
            ).fetchone()
            generation = int(previous["generation"]) + 1 if previous else 1
            acknowledged = _next_handled_results(
                connection,
                work_id=work_id,
                actor_id=actor_id,
                sources=sources,
                handled_results=handled_results,
                reprocess_results=reprocess_results,
            )
            connection.execute(
                """INSERT INTO waits(
                       work_id, generation, mode, sources_json, handled_json,
                       updated_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(work_id) DO UPDATE SET
                     generation=excluded.generation, mode=excluded.mode,
                     sources_json=excluded.sources_json,
                     handled_json=excluded.handled_json,
                     updated_at=excluded.updated_at""",
                (
                    work_id,
                    generation,
                    wait_mode,
                    _json(sources),
                    _json(sorted(acknowledged)),
                    now,
                ),
            )
            _reconcile_sources(
                project_root,
                connection,
                sources=sources,
                state_dir=state_dir,
            )
            if _effective_explicit_stop(
                connection, work=updated, actor_id=actor_id
            ) is None:
                activation = _evaluate_wait(connection, work_id)
        else:
            _persist_handled_results(
                connection,
                work_id=work_id,
                actor_id=actor_id,
                handled_results=handled_results,
            )
            connection.execute("DELETE FROM waits WHERE work_id=?", (work_id,))
            if mode == "continue" and _effective_explicit_stop(
                connection, work=updated, actor_id=actor_id
            ) is None:
                activation = _create_activation(
                    connection,
                    work=updated,
                    actor_id=actor_id,
                    reason="continue_checkpoint",
                    manifest=[],
                    not_before=(
                        datetime.now(UTC) + timedelta(seconds=delay_seconds)
                    ).isoformat(timespec="milliseconds"),
                )
        _reconcile_request_deliveries(connection, work_id=work_id)
    _publish_or_raise(project_root, state_dir=state_dir)
    return {
        **status(project_root, work_id=work_id, state_dir=state_dir),
        "activation_id": activation,
    }


def transfer(
    project_root: Path,
    *,
    work_id: str,
    from_actor: str,
    to_actor: str,
    expected_revision: int,
    reason: str,
    fenced: bool,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    work_id = _id(work_id, field="work_id")
    from_actor = _id(from_actor, field="from_actor")
    to_actor = _id(to_actor, field="to_actor")
    reason = _text(reason, field="reason", limit=MAX_SUMMARY_LENGTH)
    if not fenced:
        raise ContinuityError("ownership transfer requires explicit fencing evidence")
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        work = _require_work(connection, work_id)
        _assert_owner_revision(
            work, actor_id=from_actor, expected_revision=expected_revision
        )
        _assert_mutable_work(work)
        _require_actor(connection, to_actor)
        connection.execute(
            """UPDATE works SET owner_actor=?, revision=revision+1,
                   control_epoch=control_epoch+1, summary=?, updated_at=?
               WHERE work_id=?""",
            (to_actor, reason, core.utc_now(), work_id),
        )
        connection.execute(
            """UPDATE activations SET status='revoked'
               WHERE work_id=? AND status IN ('pending','published')""",
            (work_id,),
        )
        updated = _require_work(connection, work_id)
        activation = _create_activation(
            connection,
            work=updated,
            actor_id=to_actor,
            reason="ownership_transferred",
            manifest=[],
        )
    _publish_or_raise(project_root, state_dir=state_dir)
    return {
        **status(project_root, work_id=work_id, state_dir=state_dir),
        "activation_id": activation,
    }


def claim(
    project_root: Path,
    *,
    activation_id: str,
    actor_id: str,
    expected_epoch: int,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    activation_id = _id(activation_id, field="activation_id")
    actor_id = _id(actor_id, field="actor_id")
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        activation = connection.execute(
            "SELECT * FROM activations WHERE activation_id=?", (activation_id,)
        ).fetchone()
        if activation is None:
            raise ContinuityError(f"unknown activation: {activation_id}")
        work = _require_work(connection, activation["work_id"])
        if activation["actor_id"] != actor_id:
            raise ContinuityError("activation belongs to another actor")
        actor = _require_actor(connection, actor_id)
        if int(activation["endpoint_generation"]) != int(actor["generation"]):
            raise ContinuityError("activation endpoint generation is stale")
        if int(activation["control_epoch"]) != expected_epoch:
            raise ContinuityError("activation control epoch is stale")
        stopped = _effective_explicit_stop(
            connection, work=work, actor_id=actor_id
        )
        if stopped:
            raise ContinuityError(f"actor continuation is stopped: {stopped}")
        manifest = _decode(activation["manifest_json"], [])
        request_id = _manifest_value(manifest, "request:")
        source_activation_id = _manifest_value(manifest, "activation:")
        obligation_id = _manifest_value(manifest, "obligation:")
        obligation = None
        if obligation_id is not None:
            obligation = connection.execute(
                "SELECT * FROM obligations WHERE obligation_id=?",
                (obligation_id,),
            ).fetchone()
        linked_request = None
        if obligation_id is not None:
            linked_request = connection.execute(
                "SELECT * FROM requests WHERE obligation_id=?",
                (obligation_id,),
            ).fetchone()
            if request_id is None and linked_request is not None:
                request_id = str(linked_request["request_id"])
        is_request_message = activation["reason"] == "request_message"
        is_request_reply = activation["reason"] == "request_reply"
        is_recovery = activation["reason"] in {
            "assignment_recovery",
            "work_recovery",
        }
        is_reply_recovery = (
            activation["reason"] == "work_recovery"
            and request_id is not None
            and source_activation_id is not None
        )
        is_obligation = obligation_id is not None and not is_request_reply
        is_communication_assignment = (
            obligation is not None and linked_request is not None
        )
        if not (
            is_request_message
            or is_request_reply
            or is_reply_recovery
            or is_communication_assignment
        ):
            _assert_mutable_work(work)
        request = None
        if request_id is not None:
            request = connection.execute(
                "SELECT * FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if request is None:
                raise ContinuityError("request activation references missing request")
            expected_actor = request[
                "return_actor"
                if is_request_reply or is_reply_recovery
                else "recipient_actor"
            ]
            if expected_actor != actor_id:
                raise ContinuityError("request activation belongs to another route")
            if is_request_reply and (
                request["status"] not in {"reply_ready", "handled"}
                or request["reply_activation_id"] != activation_id
            ):
                raise ContinuityError("request reply is no longer current")
            if is_reply_recovery:
                source_activation = connection.execute(
                    "SELECT status FROM activations WHERE activation_id=?",
                    (source_activation_id,),
                ).fetchone()
                if (
                    request["status"] != "reply_ready"
                    or request["reply_activation_id"] != source_activation_id
                    or source_activation is None
                    or source_activation["status"] != "claimed"
                ):
                    raise ContinuityError(
                        "request reply recovery is no longer current"
                    )
        if is_recovery and not is_reply_recovery:
            stopped = (
                _effective_explicit_stop(connection, work=work, actor_id=actor_id)
                if is_communication_assignment
                else _effective_recovery_stop(
                    connection, work=work, actor_id=actor_id
                )
            )
            if stopped:
                raise ContinuityError(f"recovery is stopped: {stopped}")
        if is_obligation:
            if obligation is None or obligation["status"] != "open":
                raise ContinuityError("obligation assignment is no longer active")
            if int(obligation["generation"]) != int(
                activation["assignment_generation"] or 0
            ):
                raise ContinuityError("obligation assignment generation is stale")
            existing_claim = obligation["claimed_activation_id"]
            if existing_claim and existing_claim != activation_id and not is_recovery:
                return {
                    "activation_id": activation_id,
                    "status": "already_claimed",
                    "claim_activation_id": existing_claim,
                    "actionable": True,
                    "idempotent": True,
                }
        elif not (is_request_reply or is_request_message or is_reply_recovery):
            if (
                int(activation["control_epoch"]) != expected_epoch
                or int(work["control_epoch"]) != expected_epoch
                or int(activation["work_revision"]) != int(work["revision"])
                or work["owner_actor"] != actor_id
                or work["mode"] not in {"continue", "waiting"}
            ):
                raise ContinuityError("activation continuation authority is stale")
        if activation["status"] == "claimed":
            return {
                "activation_id": activation_id,
                "status": "claimed",
                "actionable": True,
                "idempotent": True,
            }
        if activation["status"] not in ACTIVATION_ACTIVE:
            raise ContinuityError(
                f"activation is not claimable: {activation['status']}"
            )
        connection.execute(
            """UPDATE activations SET status='claimed', claimed_at=?
               WHERE activation_id=?""",
            (core.utc_now(), activation_id),
        )
        if is_obligation and not is_recovery:
            now = core.utc_now()
            connection.execute(
                """UPDATE obligations
                   SET claimed_at=?, claimed_activation_id=?, claimed_generation=?
                   WHERE obligation_id=?""",
                (now, activation_id, obligation["generation"], obligation_id),
            )
            if request is not None:
                connection.execute(
                    "UPDATE requests SET status='claimed', updated_at=? "
                    "WHERE request_id=?",
                    (now, request_id),
                )
        elif is_request_message and request is not None:
            connection.execute(
                "UPDATE requests SET status='closed_no_reply', updated_at=? "
                "WHERE request_id=?",
                (core.utc_now(), request_id),
            )
        if is_recovery:
            incident = connection.execute(
                "SELECT * FROM recovery_incidents WHERE activation_id=?",
                (activation_id,),
            ).fetchone()
            if incident is None or incident["status"] != "queued":
                raise ContinuityError("recovery incident is no longer current")
            policy = connection.execute(
                "SELECT * FROM recovery_policies WHERE work_id=?",
                (work["work_id"],),
            ).fetchone()
            if policy is None:
                raise ContinuityError("recovery policy is no longer armed")
            interval = min(
                float(policy["max_interval_seconds"]),
                float(policy["interval_seconds"])
                * (2 ** min(int(incident["attempts"]), 16)),
            )
            connection.execute(
                """UPDATE recovery_incidents
                   SET status='waiting', attempts=attempts+1,
                       next_inspection_at=?, updated_at=? WHERE incident_id=?""",
                (
                    (datetime.now(UTC) + timedelta(seconds=interval)).isoformat(
                        timespec="milliseconds"
                    ),
                    core.utc_now(),
                    incident["incident_id"],
                ),
            )
    _publish_or_raise(project_root, state_dir=state_dir)
    return {
        "activation_id": activation_id,
        "status": "claimed",
        "actionable": True,
        "idempotent": False,
    }


def observe_signal(
    project_root: Path,
    signal: dict[str, Any],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> bool:
    source_key = _signal_source_key(signal)
    if source_key is None:
        return False
    retained = _retained_source_observation(
        project_root, source_key, state_dir=state_dir
    )
    status_value = str(signal.get("terminal_status") or "unknown")
    data: dict[str, Any]
    if retained["state"] == "terminal":
        status_value = str(retained["status"])
        data = retained["data"]
    else:
        data = {
            "transport_observation": {
                "state": retained["state"],
                "reason": retained.get("reason"),
            }
        }
        for key in ("result_path", "evidence_path", "event_path"):
            value = signal.get(key)
            if not isinstance(value, str):
                continue
            path = Path(value)
            if path.is_file():
                with contextlib.suppress(OSError):
                    data[key.removesuffix("_path")] = _artifact_fact(path)
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        changed, _outcome_id = _record_result(
            connection,
            source_key=source_key,
            status_value=status_value,
            event_id=str(signal.get("event_id") or "") or None,
            data=data,
        )
        if changed:
            waiting = connection.execute("SELECT work_id FROM waits").fetchall()
            for row in waiting:
                work = _require_work(connection, row["work_id"])
                if _effective_explicit_stop(
                    connection, work=work, actor_id=work["owner_actor"]
                ) is None:
                    _evaluate_wait(connection, row["work_id"])
            assignment_waits = connection.execute(
                "SELECT obligation_id FROM assignment_checkpoints "
                "WHERE mode='waiting'"
            ).fetchall()
            for row in assignment_waits:
                obligation = connection.execute(
                    "SELECT * FROM obligations WHERE obligation_id=?",
                    (row["obligation_id"],),
                ).fetchone()
                if obligation is None:
                    continue
                work = _require_work(connection, obligation["work_id"])
                if _effective_explicit_stop(
                    connection,
                    work=work,
                    actor_id=obligation["assignee_actor"],
                ) is None:
                    _evaluate_assignment_wait(connection, row["obligation_id"])
    if changed:
        _publish_or_raise(project_root, state_dir=state_dir)
    return changed


def publish_pending(
    project_root: Path, *, state_dir: str = core.DEFAULT_STATE_DIR
) -> dict[str, Any]:
    published: list[str] = []
    errors: list[dict[str, str]] = []
    with _database(project_root, state_dir=state_dir) as connection:
        rows = connection.execute(
            """SELECT o.*, a.* FROM outbox o JOIN activations a USING(activation_id)
               WHERE o.status='pending' AND a.status='pending' ORDER BY a.created_at"""
        ).fetchall()
    for row in rows:
        activation_id = row["activation_id"]
        event_id = row["event_id"]
        directory = (
            continuity_root(project_root, state_dir=state_dir)
            / "activations"
            / activation_id
        )
        result_path = directory / "result.json"
        evidence_path = directory / "evidence.json"
        result = entry_packet(
            project_root, activation_id=activation_id, state_dir=state_dir
        )
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "kind": "ORCHESTRATOR_CONTINUITY_ACTIVATION_EVIDENCE",
            "activation_id": activation_id,
            "control_epoch": row["control_epoch"],
            "endpoint_generation": row["endpoint_generation"],
            "created_at": row["created_at"],
        }
        try:
            if result_path.is_file():
                retained_result = core.load_object(result_path)
                if (
                    retained_result.get("kind")
                    != "ORCHESTRATOR_CONTINUITY_ENTRY_PACKET"
                    or retained_result.get("activation", {}).get("activation_id")
                    != activation_id
                ):
                    raise ContinuityError("retained activation result conflicts")
            else:
                core.atomic_json(result_path, result)
            if evidence_path.is_file():
                retained_evidence = core.load_object(evidence_path)
                if (
                    retained_evidence.get("kind")
                    != "ORCHESTRATOR_CONTINUITY_ACTIVATION_EVIDENCE"
                    or retained_evidence.get("activation_id") != activation_id
                ):
                    raise ContinuityError("retained activation evidence conflicts")
            else:
                core.atomic_json(evidence_path, evidence)
            core.write_followup_event(
                project_root,
                operation_id=activation_id,
                source_kind="continuity_activation",
                terminal_status="completed",
                result_path=result_path,
                evidence_path=evidence_path,
                state_dir=state_dir,
                event_id=event_id,
                wake_target=_decode(row["wake_target_json"], {}),
                not_before=row["not_before"],
            )
            with _database(project_root, state_dir=state_dir, write=True) as connection:
                connection.execute(
                    """UPDATE activations SET status='published'
                       WHERE activation_id=? AND status='pending'""",
                    (activation_id,),
                )
                connection.execute(
                    """UPDATE outbox SET status='published', attempts=attempts+1,
                           last_error=NULL, updated_at=?
                       WHERE activation_id=?""",
                    (core.utc_now(), activation_id),
                )
            published.append(activation_id)
        except (OSError, RuntimeError, ValueError) as error:
            errors.append({"activation_id": activation_id, "error": str(error)[:500]})
            with _database(project_root, state_dir=state_dir, write=True) as connection:
                connection.execute(
                    """UPDATE outbox SET attempts=attempts+1,
                           last_error=?, updated_at=? WHERE activation_id=?""",
                    (str(error)[:500], core.utc_now(), activation_id),
                )
    return {"published": published, "errors": errors}


def _publish_or_raise(
    project_root: Path, *, state_dir: str = core.DEFAULT_STATE_DIR
) -> dict[str, Any]:
    publication = publish_pending(project_root, state_dir=state_dir)
    if publication["errors"]:
        first = publication["errors"][0]
        raise ContinuityError(
            "continuity state was committed but activation publication failed; "
            "run `continuity reconcile`: " + str(first["error"])
        )
    return publication


def reconcile(
    project_root: Path, *, state_dir: str = core.DEFAULT_STATE_DIR
) -> dict[str, Any]:
    recorded_results: list[str] = []
    recovery: dict[str, Any] = {
        "queued": [],
        "suppressed": [],
        "coverage_complete": True,
    }
    with _database(project_root, state_dir=state_dir, write=True) as connection:
        waits = connection.execute("SELECT * FROM waits").fetchall()
        assignment_waits = connection.execute(
            "SELECT * FROM assignment_checkpoints WHERE mode='waiting'"
        ).fetchall()
        sources = [
            source
            for row in [*waits, *assignment_waits]
            for source in _decode(row["sources_json"], [])
        ]
        recorded_results.extend(
            _reconcile_sources(
                project_root,
                connection,
                sources=sources,
                state_dir=state_dir,
            )
        )
        for row in waits:
            work = _require_work(connection, row["work_id"])
            if _effective_explicit_stop(
                connection, work=work, actor_id=work["owner_actor"]
            ) is None:
                _evaluate_wait(connection, row["work_id"])
        for row in assignment_waits:
            obligation = connection.execute(
                "SELECT * FROM obligations WHERE obligation_id=?",
                (row["obligation_id"],),
            ).fetchone()
            if obligation is None:
                continue
            work = _require_work(connection, obligation["work_id"])
            if _effective_explicit_stop(
                connection, work=work, actor_id=obligation["assignee_actor"]
            ) is None:
                _evaluate_assignment_wait(connection, row["obligation_id"])
        _reconcile_request_deliveries(connection)
        recovery = _reconcile_recovery(connection)
    publication = publish_pending(project_root, state_dir=state_dir)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_CONTINUITY_RECONCILIATION",
        "recorded_results": recorded_results,
        "recovery": recovery,
        **publication,
    }


def delivery_disposition(
    project_root: Path,
    signal: dict[str, Any],
    *,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Return whether one retained continuity activation is still deliverable."""

    if signal.get("source_kind") != "continuity_activation":
        return {"deliver": True, "consume": False, "reason": "not_continuity"}
    activation_id = signal.get("operation_id")
    if not isinstance(activation_id, str):
        raise ContinuityError("continuity signal has no activation identity")
    with _database(project_root, state_dir=state_dir) as connection:
        activation = connection.execute(
            "SELECT status FROM activations WHERE activation_id=?",
            (activation_id,),
        ).fetchone()
    if activation is None:
        raise ContinuityError(f"continuity activation is missing: {activation_id}")
    status_value = str(activation["status"])
    return {
        "deliver": status_value == "published",
        "consume": status_value in {"claimed", "revoked"},
        "reason": f"continuity_activation_{status_value}",
    }


def self_check(
    project_root: Path, *, repair: bool = False, state_dir: str = core.DEFAULT_STATE_DIR
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    with _database(project_root, state_dir=state_dir, write=repair) as connection:
        for row in connection.execute(
            """SELECT w.work_id FROM works w
               LEFT JOIN actors a ON a.actor_id=w.owner_actor AND a.active=1
               WHERE a.actor_id IS NULL AND w.mode!='complete'"""
        ).fetchall():
            findings.append(
                {"code": "owner_endpoint_missing", "work_id": row["work_id"]}
            )
        for row in connection.execute(
            """SELECT o.obligation_id FROM obligations o
               LEFT JOIN actors a ON a.actor_id=o.assignee_actor AND a.active=1
               WHERE o.status='open' AND a.actor_id IS NULL"""
        ).fetchall():
            findings.append(
                {
                    "code": "assignee_endpoint_missing",
                    "obligation_id": row["obligation_id"],
                }
            )
        for row in connection.execute(
            """SELECT request_id FROM requests
               WHERE requires_reply=1 AND obligation_id IS NULL"""
        ).fetchall():
            findings.append(
                {
                    "code": "request_reply_obligation_missing",
                    "request_id": row["request_id"],
                }
            )
        for request in connection.execute(
            """SELECT * FROM requests
               WHERE status IN ('delivery_pending','reply_ready')"""
        ).fetchall():
            reason = (
                "request_message"
                if request["status"] == "delivery_pending"
                else "request_reply"
            )
            actor_id = request[
                "recipient_actor"
                if reason == "request_message"
                else "return_actor"
            ]
            pointer = request[
                "request_activation_id"
                if reason == "request_message"
                else "reply_activation_id"
            ]
            work = _require_work(connection, request["work_id"])
            if _effective_explicit_stop(
                connection, work=work, actor_id=actor_id
            ) is not None:
                continue
            if _active_current_activation(
                connection,
                activation_id=pointer,
                actor_id=actor_id,
                reason=reason,
            ) is None:
                findings.append(
                    {
                        "code": f"{reason}_delivery_missing",
                        "request_id": request["request_id"],
                    }
                )
                if repair:
                    _reconcile_request_deliveries(
                        connection, work_id=request["work_id"]
                    )
        for cycle in _assignment_dependency_cycles(connection):
            findings.append(
                {"code": "assignment_dependency_cycle", "obligation_ids": cycle}
            )
        waits = connection.execute("SELECT * FROM waits").fetchall()
        for row in waits:
            sources = _decode(row["sources_json"], [])
            handled = set(_decode(row["handled_json"], []))
            for source_key in sources:
                if source_key.startswith("obligation:"):
                    obligation_id = source_key.split(":", 1)[1]
                    obligation = connection.execute(
                        "SELECT status FROM obligations WHERE obligation_id=?",
                        (obligation_id,),
                    ).fetchone()
                    if obligation is None:
                        findings.append(
                            {
                                "code": "wait_source_missing",
                                "work_id": row["work_id"],
                                "source_key": source_key,
                            }
                        )
                    continue
                observation = _retained_source_observation(
                    project_root, source_key, state_dir=state_dir
                )
                if observation["state"] in {"missing", "invalid", "unsupported"}:
                    findings.append(
                        {
                            "code": f"wait_source_{observation['state']}",
                            "work_id": row["work_id"],
                            "source_key": source_key,
                            "detail": observation.get("reason")
                            or observation.get("descriptor_path"),
                        }
                    )
                elif observation["state"] == "terminal":
                    digest_value, outcome_id = _outcome_identity(
                        source_key,
                        str(observation["status"]),
                        observation["data"],
                    )
                    exists = connection.execute(
                        """SELECT outcome_id FROM results
                           WHERE source_key=? AND digest=?""",
                        (source_key, digest_value),
                    ).fetchone()
                    if exists is None:
                        findings.append(
                            {
                                "code": "terminal_source_unrecorded",
                                "work_id": row["work_id"],
                                "source_key": source_key,
                                "outcome_id": outcome_id,
                            }
                        )
                        if repair:
                            _record_result(
                                connection,
                                source_key=source_key,
                                status_value=str(observation["status"]),
                                event_id=None,
                                data=observation["data"],
                            )
            for activation in connection.execute(
                """SELECT activation_id, manifest_json FROM activations
                   WHERE work_id=? AND reason='wait_satisfied'
                     AND status='claimed'
                     AND work_revision=(SELECT revision FROM works WHERE work_id=?)
                     AND control_epoch=(SELECT control_epoch FROM works WHERE work_id=?)
                     AND endpoint_generation=(
                         SELECT generation FROM actors
                         WHERE actor_id=(SELECT owner_actor FROM works WHERE work_id=?)
                     )""",
                (row["work_id"], row["work_id"], row["work_id"], row["work_id"]),
            ).fetchall():
                unhandled = [
                    outcome_id
                    for outcome_id in _decode(activation["manifest_json"], [])
                    if outcome_id not in handled
                ]
                if unhandled:
                    findings.append(
                        {
                            "code": "claimed_result_unhandled",
                            "work_id": row["work_id"],
                            "activation_id": activation["activation_id"],
                            "outcome_ids": unhandled,
                        }
                    )
            if _wait_satisfied(connection, row["work_id"]):
                work = _require_work(connection, row["work_id"])
                if _effective_explicit_stop(
                    connection, work=work, actor_id=work["owner_actor"]
                ) is not None:
                    continue
                active = connection.execute(
                    """SELECT 1 FROM activations
                       WHERE work_id=? AND actor_id=? AND reason='wait_satisfied'
                         AND status IN ('pending','published','claimed')
                         AND work_revision=? AND control_epoch=?
                         AND endpoint_generation=?""",
                    (
                        row["work_id"],
                        work["owner_actor"],
                        work["revision"],
                        work["control_epoch"],
                        _require_actor(connection, work["owner_actor"])["generation"],
                    ),
                ).fetchone()
                if active is None:
                    findings.append(
                        {
                            "code": "satisfied_wait_not_activated",
                            "work_id": row["work_id"],
                        }
                    )
                    if repair:
                        _evaluate_wait(connection, row["work_id"])
        for assignment in connection.execute(
            "SELECT * FROM assignment_checkpoints WHERE mode='waiting'"
        ).fetchall():
            obligation = connection.execute(
                "SELECT * FROM obligations WHERE obligation_id=?",
                (assignment["obligation_id"],),
            ).fetchone()
            if obligation is None or not _assignment_continuation_allowed(
                connection, obligation=obligation
            ):
                continue
            for source_key in _decode(assignment["sources_json"], []):
                if source_key.startswith("obligation:"):
                    dependency_id = source_key.split(":", 1)[1]
                    dependency = connection.execute(
                        "SELECT 1 FROM obligations WHERE obligation_id=?",
                        (dependency_id,),
                    ).fetchone()
                    if dependency is None:
                        findings.append(
                            {
                                "code": "assignment_wait_source_missing",
                                "obligation_id": assignment["obligation_id"],
                                "source_key": source_key,
                            }
                        )
                    continue
                observation = _retained_source_observation(
                    project_root, source_key, state_dir=state_dir
                )
                if observation["state"] in {"missing", "invalid", "unsupported"}:
                    findings.append(
                        {
                            "code": f"assignment_wait_source_{observation['state']}",
                            "obligation_id": assignment["obligation_id"],
                            "source_key": source_key,
                            "detail": observation.get("reason")
                            or observation.get("descriptor_path"),
                        }
                    )
            satisfied, _manifest = _assignment_wait_satisfied(
                connection, assignment["obligation_id"]
            )
            if satisfied:
                work = _require_work(connection, obligation["work_id"])
                if _effective_explicit_stop(
                    connection,
                    work=work,
                    actor_id=obligation["assignee_actor"],
                ) is not None:
                    continue
                activation = _active_assignment_wait_activation(
                    connection, obligation=obligation
                )
                if activation is None:
                    findings.append(
                        {
                            "code": "assignment_wait_not_activated",
                            "obligation_id": assignment["obligation_id"],
                        }
                    )
                    if repair:
                        _evaluate_assignment_wait(
                            connection, assignment["obligation_id"]
                        )
        for row in connection.execute(
            """SELECT work_id, mode, owner_actor FROM works
               WHERE mode IN ('waiting','continue')"""
        ).fetchall():
            if row["mode"] == "waiting":
                has_wait = connection.execute(
                    "SELECT 1 FROM waits WHERE work_id=?", (row["work_id"],)
                ).fetchone()
                if has_wait is None:
                    findings.append(
                        {
                            "code": "waiting_checkpoint_missing",
                            "work_id": row["work_id"],
                        }
                    )
            else:
                work = _require_work(connection, row["work_id"])
                if _effective_explicit_stop(
                    connection, work=work, actor_id=row["owner_actor"]
                ) is not None:
                    continue
                route = connection.execute(
                    """SELECT 1 FROM activations
                       WHERE work_id=? AND actor_id=?
                         AND reason='continue_checkpoint'
                         AND status IN ('pending','published')""",
                    (row["work_id"], row["owner_actor"]),
                ).fetchone()
                if route is None:
                    findings.append(
                        {
                            "code": "continuation_route_missing",
                            "work_id": row["work_id"],
                        }
                    )
                    if repair:
                        work = _require_work(connection, row["work_id"])
                        _create_activation(
                            connection,
                            work=work,
                            actor_id=row["owner_actor"],
                            reason="continue_checkpoint",
                            manifest=[],
                        )
        for row in connection.execute(
            """SELECT activation_id, attempts, last_error FROM outbox
               WHERE status='pending' AND attempts>0"""
        ).fetchall():
            findings.append({"code": "outbox_delivery_failed", **dict(row)})
        observed_findings = findings
        actionable_findings: list[dict[str, Any]] = []
        resolved_findings: list[dict[str, Any]] = []
        now = core.utc_now()
        identified_findings: list[tuple[str, str, dict[str, Any]]] = []
        for finding in observed_findings:
            finding_digest = _digest(finding)
            identity = finding_digest[0:24]
            identified_findings.append((identity, finding_digest, finding))
            existing = connection.execute(
                "SELECT * FROM diagnostics WHERE diagnostic_id=?",
                (identity,),
            ).fetchone()
            if (
                existing is not None
                and existing["status"] == "resolved"
                and existing["finding_digest"] == finding_digest
            ):
                resolved_findings.append(finding)
            else:
                actionable_findings.append(finding)
        if repair:
            active_ids: set[str] = set()
            resolved_identities = {
                _digest(finding)[0:24] for finding in resolved_findings
            }
            for identity, finding_digest, finding in identified_findings:
                active_ids.add(identity)
                if identity in resolved_identities:
                    continue
                connection.execute(
                    """INSERT INTO diagnostics(
                           diagnostic_id, code, subject, status, detail,
                           updated_at, finding_digest, resolution_json
                       ) VALUES(?,?,?,'open',?,?,?,NULL)
                       ON CONFLICT(diagnostic_id) DO UPDATE SET
                         status='open', detail=excluded.detail,
                         updated_at=excluded.updated_at,
                         finding_digest=excluded.finding_digest,
                         resolution_json=NULL""",
                    (
                        identity,
                        finding["code"],
                        str(finding),
                        _json(finding),
                        now,
                        finding_digest,
                    ),
                )
            if active_ids:
                marks = ",".join("?" for _ in active_ids)
                connection.execute(
                    f"""UPDATE diagnostics SET status='resolved', updated_at=?
                        WHERE status='open'
                          AND diagnostic_id NOT IN ({marks})""",
                    (now, *sorted(active_ids)),
                )
            else:
                connection.execute(
                    """UPDATE diagnostics SET status='resolved', updated_at=?
                       WHERE status='open'""",
                    (now,),
                )
    publication = (
        publish_pending(project_root, state_dir=state_dir)
        if repair
        else {"published": [], "errors": []}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_CONTINUITY_SELF_CHECK",
        "status": "ok" if not actionable_findings else "findings",
        "finding_count": len(actionable_findings),
        "findings": actionable_findings,
        "observed_finding_count": len(observed_findings),
        "resolved_finding_count": len(resolved_findings),
        "resolved_findings": resolved_findings,
        "repair": repair,
        **publication,
    }


def entry_packet(
    project_root: Path,
    *,
    activation_id: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    activation_id = _id(activation_id, field="activation_id")
    with _database(project_root, state_dir=state_dir) as connection:
        activation = connection.execute(
            "SELECT * FROM activations WHERE activation_id=?", (activation_id,)
        ).fetchone()
        if activation is None:
            raise ContinuityError(f"unknown activation: {activation_id}")
        work = _require_work(connection, activation["work_id"])
        manifest = set(_decode(activation["manifest_json"], []))
        manifest_obligations = sorted(
            value.split(":", 1)[1]
            for value in manifest
            if value.startswith("obligation:")
        )
        if manifest_obligations:
            marks = ",".join("?" for _ in manifest_obligations)
            priority = f"CASE WHEN obligation_id IN ({marks}) THEN 0 "
            arguments: tuple[Any, ...] = (
                work["work_id"],
                *manifest_obligations,
                MAX_ENTRY_OBLIGATIONS,
            )
        else:
            priority = "CASE WHEN 0 THEN 0 "
            arguments = (work["work_id"], MAX_ENTRY_OBLIGATIONS)
        obligations = connection.execute(
            "SELECT * FROM obligations WHERE work_id=? ORDER BY "
            + priority
            + "WHEN required=1 AND status='open' THEN 1 ELSE 2 END, obligation_id "
            "LIMIT ?",
            arguments,
        ).fetchall()
        counts = connection.execute(
            """SELECT COUNT(*) AS total_count,
                      SUM(CASE WHEN required=1 AND status='open' THEN 1 ELSE 0 END)
                          AS open_required_count
               FROM obligations WHERE work_id=?""",
            (work["work_id"],),
        ).fetchone()
        total_count = int(counts["total_count"] or 0)
        open_required_count = int(counts["open_required_count"] or 0)
        wait = connection.execute(
            "SELECT * FROM waits WHERE work_id=?", (work["work_id"],)
        ).fetchone()
        outcome_ids = [value for value in manifest if value.startswith("result-")]
        managed_results = []
        if outcome_ids:
            marks = ",".join("?" for _ in outcome_ids)
            managed_results = connection.execute(
                f"SELECT * FROM results WHERE outcome_id IN ({marks}) "
                "ORDER BY recorded_at, outcome_id",
                tuple(outcome_ids),
            ).fetchall()
        note = connection.execute(
            "SELECT * FROM notes WHERE work_id=?", (work["work_id"],)
        ).fetchone()
        request_ids = sorted(
            value.split(":", 1)[1]
            for value in manifest
            if value.startswith("request:")
        )
        if request_ids:
            marks = ",".join("?" for _ in request_ids)
            requests = connection.execute(
                f"SELECT * FROM requests WHERE request_id IN ({marks}) "
                "ORDER BY request_id",
                tuple(request_ids),
            ).fetchall()
        else:
            requests = connection.execute(
                """SELECT * FROM requests WHERE work_id=?
                   AND status IN ('delivery_pending','claimed','reply_ready')
                   ORDER BY request_id LIMIT ?""",
                (work["work_id"], MAX_STATUS_REQUESTS),
            ).fetchall()
            request_ids = [str(row["request_id"]) for row in requests]
        if request_ids:
            marks = ",".join("?" for _ in request_ids)
            responses = connection.execute(
                f"SELECT * FROM responses WHERE request_id IN ({marks}) "
                "ORDER BY created_at DESC, response_id DESC LIMIT ?",
                (*request_ids, MAX_STATUS_RESPONSES),
            ).fetchall()
            response_total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM responses "
                    f"WHERE request_id IN ({marks})",
                    tuple(request_ids),
                ).fetchone()[0]
            )
        else:
            responses = []
            response_total = 0
        obligation_ids = [row["obligation_id"] for row in obligations]
        if obligation_ids:
            marks = ",".join("?" for _ in obligation_ids)
            assignment_checkpoints = connection.execute(
                f"SELECT * FROM assignment_checkpoints "
                f"WHERE obligation_id IN ({marks}) ORDER BY obligation_id",
                tuple(obligation_ids),
            ).fetchall()
        else:
            assignment_checkpoints = []
        recovery_incidents = connection.execute(
            """SELECT * FROM recovery_incidents WHERE work_id=?
               AND status!='resolved' ORDER BY updated_at DESC LIMIT 16""",
            (work["work_id"],),
        ).fetchall()
        recovery_incident_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM recovery_incidents "
                "WHERE work_id=? AND status!='resolved'",
                (work["work_id"],),
            ).fetchone()[0]
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ORCHESTRATOR_CONTINUITY_ENTRY_PACKET",
        "activation": _activation_dict(activation),
        "work": _work_dict(work),
        "obligations": [_obligation_dict(row) for row in obligations],
        "wait": _wait_dict(wait) if wait is not None else None,
        "managed_results": [_result_dict(row) for row in managed_results],
        "requests": [_request_dict(row) for row in requests],
        "responses": [_response_dict(row) for row in responses],
        "response_summary": {
            "total_count": response_total,
            "included_count": len(responses),
            "truncated": len(responses) < response_total,
        },
        "assignment_checkpoints": [
            _assignment_checkpoint_dict(row) for row in assignment_checkpoints
        ],
        "recovery_incidents": [dict(row) for row in recovery_incidents],
        "recovery_summary": {
            "active_count": recovery_incident_total,
            "included_count": len(recovery_incidents),
            "truncated": len(recovery_incidents) < recovery_incident_total,
        },
        "operational_note": _note_dict(note) if note is not None else None,
        "obligation_summary": {
            "total_count": total_count,
            "included_count": len(obligations),
            "open_required_count": open_required_count,
            "truncated": len(obligations) < total_count,
        },
        "repository_snapshot": _repository_snapshot(project_root),
        "claim_command": (
            "orchestrator-engine continuity claim "
            f"--activation-id {activation_id} --actor-id {activation['actor_id']} "
            f"--expected-epoch {activation['control_epoch']}"
        ),
        "action_contract": {
            "request_terminal_reply": "continuity request-respond --kind terminal",
            "assignment_progress": (
                "continuity assignment-checkpoint "
                "[--handled-result OUTCOME_ID]"
            ),
            "reply_handling": "continuity request-handle",
            "recovery_semantics": (
                "Inspect current durable state; do not repeat completed work or infer "
                "abandonment from elapsed time."
            ),
        },
    }


def status(
    project_root: Path,
    *,
    work_id: str | None = None,
    obligation_limit: int = MAX_STATUS_OBLIGATIONS,
    obligation_cursor: str | None = None,
    result_limit: int = MAX_STATUS_RESULTS,
    result_cursor: str | None = None,
    request_limit: int = MAX_STATUS_REQUESTS,
    request_cursor: str | None = None,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    if work_id is not None:
        work_id = _id(work_id, field="work_id")
    if obligation_cursor is not None:
        obligation_cursor = _id(obligation_cursor, field="obligation_cursor")
    if result_cursor is not None:
        result_cursor = _id(result_cursor, field="result_cursor")
    if request_cursor is not None:
        request_cursor = _id(request_cursor, field="request_cursor")
    if obligation_limit < 1 or obligation_limit > MAX_STATUS_OBLIGATIONS:
        raise ContinuityError(
            f"obligation_limit must be between 1 and {MAX_STATUS_OBLIGATIONS}"
        )
    if result_limit < 1 or result_limit > MAX_STATUS_RESULTS:
        raise ContinuityError(
            f"result_limit must be between 1 and {MAX_STATUS_RESULTS}"
        )
    if request_limit < 1 or request_limit > MAX_STATUS_REQUESTS:
        raise ContinuityError(
            f"request_limit must be between 1 and {MAX_STATUS_REQUESTS}"
        )
    with _database(project_root, state_dir=state_dir) as connection:
        works = connection.execute(
            "SELECT * FROM works"
            + (" WHERE work_id=?" if work_id else "")
            + " ORDER BY work_id",
            ((work_id,) if work_id else ()),
        ).fetchall()
        obligation_conditions: list[str] = []
        obligation_arguments: list[Any] = []
        if work_id:
            obligation_conditions.append("work_id=?")
            obligation_arguments.append(work_id)
        if obligation_cursor:
            obligation_conditions.append("obligation_id>?")
            obligation_arguments.append(obligation_cursor)
        obligation_where = (
            " WHERE " + " AND ".join(obligation_conditions)
            if obligation_conditions
            else ""
        )
        obligations = connection.execute(
            "SELECT * FROM obligations"
            + obligation_where
            + " ORDER BY obligation_id LIMIT ?",
            (*obligation_arguments, obligation_limit + 1),
        ).fetchall()
        obligation_total = connection.execute(
            "SELECT COUNT(*) AS count FROM obligations"
            + (" WHERE work_id=?" if work_id else ""),
            ((work_id,) if work_id else ()),
        ).fetchone()["count"]
        activations = connection.execute(
            "SELECT * FROM activations"
            + (" WHERE work_id=?" if work_id else "")
            + " ORDER BY created_at DESC LIMIT ?",
            (
                (work_id, MAX_STATUS_ACTIVATIONS)
                if work_id
                else (MAX_STATUS_ACTIVATIONS,)
            ),
        ).fetchall()
        waits = connection.execute(
            "SELECT * FROM waits"
            + (" WHERE work_id=?" if work_id else "")
            + " ORDER BY work_id",
            ((work_id,) if work_id else ()),
        ).fetchall()
        source_keys = sorted(
            {
                source
                for wait in waits
                for source in _decode(wait["sources_json"], [])
            }
        )
        if source_keys:
            marks = ",".join("?" for _ in source_keys)
            results = connection.execute(
                f"SELECT * FROM results WHERE source_key IN ({marks}) "
                + ("AND outcome_id>? " if result_cursor else "")
                + "ORDER BY outcome_id LIMIT ?",
                (
                    *source_keys,
                    *((result_cursor,) if result_cursor else ()),
                    result_limit + 1,
                ),
            ).fetchall()
            result_total = connection.execute(
                f"SELECT COUNT(*) AS count FROM results "
                f"WHERE source_key IN ({marks})",
                tuple(source_keys),
            ).fetchone()["count"]
        else:
            results = []
            result_total = 0
        notes = connection.execute(
            "SELECT * FROM notes"
            + (" WHERE work_id=?" if work_id else "")
            + " ORDER BY work_id",
            ((work_id,) if work_id else ()),
        ).fetchall()
        diagnostics = connection.execute(
            "SELECT * FROM diagnostics WHERE status='open' ORDER BY diagnostic_id"
        ).fetchall()
        request_conditions: list[str] = []
        request_arguments: list[Any] = []
        if work_id:
            request_conditions.append("work_id=?")
            request_arguments.append(work_id)
        if request_cursor:
            request_conditions.append("request_id>?")
            request_arguments.append(request_cursor)
        request_where = (
            " WHERE " + " AND ".join(request_conditions)
            if request_conditions
            else ""
        )
        requests = connection.execute(
            "SELECT * FROM requests"
            + request_where
            + " ORDER BY request_id LIMIT ?",
            (*request_arguments, request_limit + 1),
        ).fetchall()
        request_total = connection.execute(
            "SELECT COUNT(*) AS count FROM requests"
            + (" WHERE work_id=?" if work_id else ""),
            ((work_id,) if work_id else ()),
        ).fetchone()["count"]
        request_page_ids = [row["request_id"] for row in requests[:request_limit]]
        if request_page_ids:
            marks = ",".join("?" for _ in request_page_ids)
            responses = connection.execute(
                f"SELECT * FROM responses WHERE request_id IN ({marks}) "
                "ORDER BY created_at DESC, response_id DESC LIMIT ?",
                (*request_page_ids, MAX_STATUS_RESPONSES),
            ).fetchall()
            response_total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM responses "
                    f"WHERE request_id IN ({marks})",
                    tuple(request_page_ids),
                ).fetchone()[0]
            )
        else:
            responses = []
            response_total = 0
        assignment_checkpoints = connection.execute(
            """SELECT c.* FROM assignment_checkpoints c
               JOIN obligations o USING(obligation_id)"""
            + (" WHERE o.work_id=?" if work_id else "")
            + " ORDER BY c.obligation_id",
            ((work_id,) if work_id else ()),
        ).fetchall()
        recovery_policies = connection.execute(
            "SELECT * FROM recovery_policies"
            + (" WHERE work_id=?" if work_id else "")
            + " ORDER BY work_id",
            ((work_id,) if work_id else ()),
        ).fetchall()
        recovery_controls = connection.execute(
            "SELECT * FROM recovery_controls ORDER BY scope_kind, scope_id"
        ).fetchall()
        recovery_incidents = connection.execute(
            "SELECT * FROM recovery_incidents"
            + (" WHERE work_id=?" if work_id else "")
            + " ORDER BY updated_at DESC LIMIT 128",
            ((work_id,) if work_id else ()),
        ).fetchall()
        recovery_defaults = {
            row["key"]: row["value"]
            for row in connection.execute(
                "SELECT key, value FROM metadata WHERE key LIKE 'default_recovery_%'"
            ).fetchall()
        }
    if work_id and not works:
        raise ContinuityError(f"unknown work: {work_id}")
    has_more_obligations = len(obligations) > obligation_limit
    obligation_page = obligations[:obligation_limit]
    has_more_results = len(results) > result_limit
    result_page = results[:result_limit]
    has_more_requests = len(requests) > request_limit
    request_page = requests[:request_limit]
    source_observations = [
        _retained_source_observation(
            project_root, source_key, state_dir=state_dir
        )
        for source_key in source_keys
        if not source_key.startswith("obligation:")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "works": [_work_dict(row) for row in works],
        "obligations": [_obligation_dict(row) for row in obligation_page],
        "obligation_page": {
            "total_count": int(obligation_total),
            "included_count": len(obligation_page),
            "truncated": has_more_obligations,
            "next_cursor": (
                obligation_page[-1]["obligation_id"]
                if has_more_obligations and obligation_page
                else None
            ),
        },
        "activations": [_activation_dict(row) for row in activations],
        "waits": [_wait_dict(row) for row in waits],
        "managed_results": [_result_dict(row) for row in result_page],
        "result_page": {
            "total_count": int(result_total),
            "included_count": len(result_page),
            "truncated": has_more_results,
            "next_cursor": (
                result_page[-1]["outcome_id"]
                if has_more_results and result_page
                else None
            ),
        },
        "source_observations": source_observations,
        "requests": [_request_dict(row) for row in request_page],
        "request_page": {
            "total_count": int(request_total),
            "included_count": len(request_page),
            "truncated": has_more_requests,
            "next_cursor": (
                request_page[-1]["request_id"]
                if has_more_requests and request_page
                else None
            ),
        },
        "responses": [_response_dict(row) for row in responses],
        "response_summary": {
            "total_count": response_total,
            "included_count": len(responses),
            "truncated": len(responses) < response_total,
        },
        "assignment_checkpoints": [
            _assignment_checkpoint_dict(row) for row in assignment_checkpoints
        ],
        "recovery": {
            "defaults": recovery_defaults,
            "policies": [dict(row) for row in recovery_policies],
            "controls": [dict(row) for row in recovery_controls],
            "incidents": [dict(row) for row in recovery_incidents],
        },
        "operational_notes": [_note_dict(row) for row in notes],
        "diagnostics": [dict(row) for row in diagnostics],
    }


def _message_value(
    project_root: Path,
    *,
    message: str | None,
    message_ref: str | None,
) -> dict[str, Any]:
    if (message is None) == (message_ref is None):
        raise ContinuityError("provide exactly one inline message or message reference")
    if message is not None:
        text = _text(message, field="message", limit=MAX_MESSAGE_LENGTH)
        return {
            "kind": "inline",
            "text": text,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    reference = _text(
        message_ref, field="message_ref", limit=MAX_RESULT_REF_LENGTH
    )
    return {"kind": "reference", **_result_reference(project_root, reference)}


def _message_reference(message: dict[str, Any]) -> dict[str, str]:
    if message.get("kind") == "inline":
        digest = str(message["content_sha256"])
        return {"reference": f"inline:sha256:{digest}", "digest": digest}
    return {
        "reference": str(message["reference"]),
        "digest": str(
            message.get("content_sha256") or message.get("reference_sha256")
        ),
    }


def _actor_route(actor: sqlite3.Row) -> dict[str, Any]:
    endpoint = _decode(actor["endpoint_json"], {})
    wake_target = binding.wake_target_from_binding(endpoint)
    wake_target.pop("captured_at", None)
    return {
        "actor_id": actor["actor_id"],
        "endpoint_generation": int(actor["generation"]),
        "endpoint": endpoint,
        "wake_target": wake_target,
    }


def _request_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **{
            key: row[key]
            for key in row.keys()  # noqa: SIM118
            if not key.endswith("_json")
        },
        "requires_reply": bool(row["requires_reply"]),
        "required": bool(row["required"]),
        "message": _decode(row["message_json"], {}),
        "recipient_route": _decode(row["recipient_route_json"], {}),
        "return_route": _decode(row["return_route_json"], {}),
    }


def _response_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **{
            key: row[key]
            for key in row.keys()  # noqa: SIM118
            if not key.endswith("_json")
        },
        "content": _decode(row["content_json"], {}),
    }


def _manifest_value(manifest: list[str], prefix: str) -> str | None:
    values = [value.split(":", 1)[1] for value in manifest if value.startswith(prefix)]
    if len(values) > 1:
        raise ContinuityError(f"activation manifest contains duplicate {prefix[:-1]}")
    return values[0] if values else None


def _assert_current_assignment_claim(
    connection: sqlite3.Connection,
    *,
    obligation: sqlite3.Row,
    actor_id: str,
    activation_id: str,
) -> sqlite3.Row:
    if obligation["assignee_actor"] != actor_id:
        raise ContinuityError("only the assigned actor may update the assignment")
    if (
        obligation["claimed_activation_id"] != activation_id
        or int(obligation["claimed_generation"] or 0)
        != int(obligation["generation"])
    ):
        raise ContinuityError("operation requires the current claimed assignment")
    activation = connection.execute(
        "SELECT * FROM activations WHERE activation_id=?", (activation_id,)
    ).fetchone()
    actor = _require_actor(connection, actor_id)
    if (
        activation is None
        or activation["status"] != "claimed"
        or activation["actor_id"] != actor_id
        or int(activation["endpoint_generation"]) != int(actor["generation"])
        or int(activation["assignment_generation"] or 0)
        != int(obligation["generation"])
    ):
        raise ContinuityError("assignment claim is stale or unavailable")
    return activation


def _revoke_obligation_activations(
    connection: sqlite3.Connection, *, obligation_id: str
) -> None:
    marker = f"obligation:{obligation_id}"
    for activation in connection.execute(
        "SELECT activation_id, manifest_json FROM activations "
        "WHERE status IN ('pending','published')"
    ).fetchall():
        if marker in _decode(activation["manifest_json"], []):
            connection.execute(
                "UPDATE activations SET status='revoked' WHERE activation_id=?",
                (activation["activation_id"],),
            )


def _revoke_request_activations(
    connection: sqlite3.Connection, *, request_id: str, actor_id: str
) -> None:
    marker = f"request:{request_id}"
    for activation in connection.execute(
        "SELECT activation_id, manifest_json FROM activations "
        "WHERE actor_id=? AND status IN ('pending','published')",
        (actor_id,),
    ).fetchall():
        if marker in _decode(activation["manifest_json"], []):
            connection.execute(
                "UPDATE activations SET status='revoked' WHERE activation_id=?",
                (activation["activation_id"],),
            )


def _validate_recovery_intervals(interval: float, maximum: float) -> None:
    if interval < 10 or interval > 86400:
        raise ContinuityError("recovery interval must be between 10 and 86400")
    if maximum < interval or maximum > 604800:
        raise ContinuityError(
            "maximum recovery interval must be at least the interval and at most 604800"
        )


def _assignment_dependency_cycles(connection: sqlite3.Connection) -> list[list[str]]:
    graph: dict[str, set[str]] = {}
    for row in connection.execute(
        "SELECT obligation_id, sources_json FROM assignment_checkpoints "
        "WHERE mode='waiting'"
    ).fetchall():
        graph[str(row["obligation_id"])] = {
            source.split(":", 1)[1]
            for source in _decode(row["sources_json"], [])
            if source.startswith("obligation:")
        }
    cycles: set[tuple[str, ...]] = set()
    state: dict[str, int] = {}
    for root in sorted(graph):
        if state.get(root, 0) != 0:
            continue
        path: list[str] = [root]
        state[root] = 1
        frames: list[tuple[str, Iterator[str]]] = [
            (root, iter(sorted(graph[root])))
        ]
        while frames:
            node, dependencies = frames[-1]
            try:
                dependency = next(dependencies)
            except StopIteration:
                frames.pop()
                path.pop()
                state[node] = 2
                continue
            if dependency not in graph:
                continue
            dependency_state = state.get(dependency, 0)
            if dependency_state == 0:
                state[dependency] = 1
                path.append(dependency)
                frames.append((dependency, iter(sorted(graph[dependency]))))
            elif dependency_state == 1:
                cycle = path[path.index(dependency) :]
                rotations = [
                    tuple(cycle[index:] + cycle[:index])
                    for index in range(len(cycle))
                ]
                cycles.add(min(rotations))
    return [list(cycle) for cycle in sorted(cycles)]


def _require_actor(connection: sqlite3.Connection, actor_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM actors WHERE actor_id=? AND active=1", (actor_id,)
    ).fetchone()
    if row is None:
        raise ContinuityError(f"unknown active actor: {actor_id}")
    return row


def _require_work(connection: sqlite3.Connection, work_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM works WHERE work_id=?", (work_id,)
    ).fetchone()
    if row is None:
        raise ContinuityError(f"unknown work: {work_id}")
    return row


def _assert_owner_revision(
    work: sqlite3.Row, *, actor_id: str, expected_revision: int
) -> None:
    if work["owner_actor"] != actor_id:
        raise ContinuityError("actor does not own this work")
    if int(work["revision"]) != expected_revision:
        raise ContinuityError(
            "stale work revision: "
            f"expected {expected_revision}, current {work['revision']}"
        )


def _assert_mutable_work(work: sqlite3.Row) -> None:
    if work["mode"] == "complete":
        raise ContinuityError("completed work is immutable")


def _next_handled_results(
    connection: sqlite3.Connection,
    *,
    work_id: str,
    actor_id: str,
    sources: list[str],
    handled_results: list[str],
    reprocess_results: list[str],
) -> set[str]:
    selected_sources = set(sources)
    previous = {
        str(row["outcome_id"])
        for row in connection.execute(
            "SELECT outcome_id FROM handled_results WHERE work_id=?",
            (work_id,),
        ).fetchall()
    }
    requested = set(handled_results) | set(reprocess_results)
    resolved = _resolve_outcome_aliases(connection, requested)
    handled_results = [resolved[value] for value in handled_results]
    reprocess_results = [resolved[value] for value in reprocess_results]
    candidates = previous | set(handled_results) | set(reprocess_results)
    if not candidates:
        return set()
    marks = ",".join("?" for _ in candidates)
    rows = connection.execute(
        f"SELECT outcome_id, source_key FROM results WHERE outcome_id IN ({marks})",
        tuple(sorted(candidates)),
    ).fetchall()
    by_id = {row["outcome_id"]: row["source_key"] for row in rows}
    missing = (set(handled_results) | set(reprocess_results)) - set(by_id)
    if missing:
        raise ContinuityError(
            "unknown managed result identity: " + ", ".join(sorted(missing))
        )
    acknowledged = set(previous)
    for outcome_id in handled_results:
        if by_id[outcome_id] not in selected_sources:
            raise ContinuityError("handled result is outside the current wait")
        claimed = False
        for row in connection.execute(
            """SELECT manifest_json FROM activations
               WHERE work_id=? AND actor_id=? AND reason='wait_satisfied'
                 AND status='claimed'""",
            (work_id, actor_id),
        ).fetchall():
            if outcome_id in _decode(row["manifest_json"], []):
                claimed = True
                break
        if not claimed:
            raise ContinuityError(
                "handled result requires a claimed activation containing it"
            )
        connection.execute(
            """INSERT OR REPLACE INTO handled_results(
                   work_id, outcome_id, source_key, actor_id, handled_at
               ) VALUES(?,?,?,?,?)""",
            (work_id, outcome_id, by_id[outcome_id], actor_id, core.utc_now()),
        )
        acknowledged.add(outcome_id)
    for outcome_id in reprocess_results:
        if by_id[outcome_id] not in selected_sources:
            raise ContinuityError("reprocessed result is outside the current wait")
        connection.execute(
            "DELETE FROM handled_results WHERE work_id=? AND outcome_id=?",
            (work_id, outcome_id),
        )
        acknowledged.discard(outcome_id)
    return {
        outcome_id
        for outcome_id in acknowledged
        if by_id.get(outcome_id) in selected_sources
    }


def _next_assignment_handled_results(
    connection: sqlite3.Connection,
    *,
    obligation_id: str,
    actor_id: str,
    sources: list[str],
    handled_results: list[str],
    reprocess_results: list[str],
) -> set[str]:
    selected_sources = set(sources)
    previous = {
        str(row["outcome_id"])
        for row in connection.execute(
            "SELECT outcome_id FROM assignment_handled_results "
            "WHERE obligation_id=?",
            (obligation_id,),
        ).fetchall()
    }
    requested = set(handled_results) | set(reprocess_results)
    resolved = _resolve_outcome_aliases(connection, requested)
    handled_results = [resolved[value] for value in handled_results]
    reprocess_results = [resolved[value] for value in reprocess_results]
    candidates = previous | set(handled_results) | set(reprocess_results)
    if not candidates:
        return set()
    marks = ",".join("?" for _ in candidates)
    rows = connection.execute(
        f"SELECT outcome_id, source_key FROM results WHERE outcome_id IN ({marks})",
        tuple(sorted(candidates)),
    ).fetchall()
    by_id = {str(row["outcome_id"]): str(row["source_key"]) for row in rows}
    missing = (set(handled_results) | set(reprocess_results)) - set(by_id)
    if missing:
        raise ContinuityError(
            "unknown managed result identity: " + ", ".join(sorted(missing))
        )
    marker = f"obligation:{obligation_id}"
    claimed_manifests = [
        set(_decode(row["manifest_json"], []))
        for row in connection.execute(
            """SELECT manifest_json FROM activations
               WHERE actor_id=? AND reason='assignment_wait_satisfied'
                 AND status='claimed'""",
            (actor_id,),
        ).fetchall()
        if marker in _decode(row["manifest_json"], [])
    ]
    acknowledged = set(previous)
    for outcome_id in handled_results:
        if by_id[outcome_id] not in selected_sources:
            raise ContinuityError("handled result is outside the assignment wait")
        if not any(outcome_id in manifest for manifest in claimed_manifests):
            raise ContinuityError(
                "handled result requires the claimed assignment wait activation"
            )
        connection.execute(
            """INSERT OR REPLACE INTO assignment_handled_results(
                   obligation_id, outcome_id, source_key, actor_id, handled_at
               ) VALUES(?,?,?,?,?)""",
            (
                obligation_id,
                outcome_id,
                by_id[outcome_id],
                actor_id,
                core.utc_now(),
            ),
        )
        acknowledged.add(outcome_id)
    for outcome_id in reprocess_results:
        if by_id[outcome_id] not in selected_sources:
            raise ContinuityError("reprocessed result is outside the assignment wait")
        connection.execute(
            "DELETE FROM assignment_handled_results "
            "WHERE obligation_id=? AND outcome_id=?",
            (obligation_id, outcome_id),
        )
        acknowledged.discard(outcome_id)
    return {
        outcome_id
        for outcome_id in acknowledged
        if by_id.get(outcome_id) in selected_sources
    }


def _persist_handled_results(
    connection: sqlite3.Connection,
    *,
    work_id: str,
    actor_id: str,
    handled_results: list[str],
) -> None:
    if not handled_results:
        return
    resolved = _resolve_outcome_aliases(connection, set(handled_results))
    handled_results = [resolved[value] for value in handled_results]
    marks = ",".join("?" for _ in handled_results)
    rows = connection.execute(
        f"SELECT outcome_id, source_key FROM results WHERE outcome_id IN ({marks})",
        tuple(sorted(handled_results)),
    ).fetchall()
    by_id = {str(row["outcome_id"]): str(row["source_key"]) for row in rows}
    missing = set(handled_results) - set(by_id)
    if missing:
        raise ContinuityError(
            "unknown managed result identity: " + ", ".join(sorted(missing))
        )
    claimed_manifests = [
        set(_decode(row["manifest_json"], []))
        for row in connection.execute(
            """SELECT manifest_json FROM activations
               WHERE work_id=? AND actor_id=? AND reason='wait_satisfied'
                 AND status='claimed'""",
            (work_id, actor_id),
        ).fetchall()
    ]
    for outcome_id in handled_results:
        if not any(outcome_id in manifest for manifest in claimed_manifests):
            raise ContinuityError(
                "handled result requires a claimed activation containing it"
            )
        connection.execute(
            """INSERT OR REPLACE INTO handled_results(
                   work_id, outcome_id, source_key, actor_id, handled_at
               ) VALUES(?,?,?,?,?)""",
            (work_id, outcome_id, by_id[outcome_id], actor_id, core.utc_now()),
        )


def _resolve_outcome_aliases(
    connection: sqlite3.Connection, outcome_ids: set[str]
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for outcome_id in outcome_ids:
        row = connection.execute(
            "SELECT outcome_id FROM results WHERE outcome_id=?", (outcome_id,)
        ).fetchone()
        if row is None:
            row = connection.execute(
                """SELECT outcome_id FROM result_aliases
                   WHERE alias_outcome_id=?""",
                (outcome_id,),
            ).fetchone()
        if row is None:
            raise ContinuityError(f"unknown managed result identity: {outcome_id}")
        resolved[outcome_id] = str(row["outcome_id"])
    return resolved


def _source_key(value: str) -> str:
    kind, separator, identity = value.partition(":")
    if not separator or kind not in SOURCE_KINDS:
        raise ContinuityError("wait source must use KIND:ID with a supported kind")
    return f"{kind}:{_id(identity, field='source identity')}"


def _signal_source_key(signal: dict[str, Any]) -> str | None:
    if signal.get("kind") == "LOCAL_AI_WORKER_FINISHED":
        identity = signal.get("task_id")
        return (
            f"worker:{_id(identity, field='worker task identity')}"
            if isinstance(identity, str) and identity
            else None
        )
    source_kind = signal.get("source_kind")
    kind = SOURCE_KIND_MAP.get(str(source_kind))
    identity = signal.get("operation_id")
    if kind and isinstance(identity, str) and identity:
        return f"{kind}:{_id(identity, field='operation identity')}"
    return None


def _artifact_fact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": core.sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _retained_source_observation(
    project_root: Path,
    source_key: str,
    *,
    state_dir: str,
) -> dict[str, Any]:
    kind, identity = source_key.split(":", 1)
    project = project_root.expanduser().resolve()
    state = core.state_root(project, state_dir=state_dir)
    descriptor_path: Path
    result_path: Path
    evidence_path: Path
    expected_result_kind: str
    expected_descriptor_kind: str
    try:
        if kind == "worker":
            from . import workers

            directory = workers.task_dir_for(project, identity, state_dir=state_dir)
            descriptor_path = directory / "task.json"
            result_path = directory / "result.json"
            evidence_path = directory / "evidence.json"
            expected_result_kind = "WORKER_RESULT"
            expected_descriptor_kind = workers.TASK_KIND
        elif kind == "check":
            from . import local_checks, verification

            directory = local_checks.check_dir(project, identity, state_dir=state_dir)
            descriptor_path = directory / "check.json"
            result_path = directory / "verification-result.json"
            evidence_path = directory / "evidence.json"
            expected_result_kind = verification.VERIFICATION_RESULT_KIND
            expected_descriptor_kind = local_checks.CHECK_KIND
        elif kind == "ci":
            from . import github_actions, verification

            directory = github_actions.monitor_dir_for(
                project, identity, state_dir=state_dir
            )
            descriptor_path = directory / "monitor.json"
            result_path = state / "checks" / identity / "verification-result.json"
            evidence_path = directory / "evidence.json"
            expected_result_kind = verification.VERIFICATION_RESULT_KIND
            expected_descriptor_kind = github_actions.MONITOR_KIND
        elif kind == "pr":
            from . import github_pull_requests, verification

            directory = github_pull_requests.monitor_dir_for(
                project, identity, state_dir=state_dir
            )
            descriptor_path = directory / "monitor.json"
            result_path = state / "checks" / identity / "verification-result.json"
            evidence_path = directory / "evidence.json"
            expected_result_kind = verification.VERIFICATION_RESULT_KIND
            expected_descriptor_kind = github_pull_requests.MONITOR_KIND
        elif kind == "workstream":
            from . import workstreams

            parsed = workstreams.parse_continuation_operation_id(identity)
            if parsed is None:
                return {
                    "source_key": source_key,
                    "state": "invalid",
                    "reason": "invalid workstream continuation identity",
                }
            workstream_id, checkpoint_id = parsed
            descriptor_path = workstreams.checkpoint_path(
                project,
                workstream_id,
                checkpoint_id,
                state_dir=state_dir,
            )
            result_path, evidence_path = workstreams._checkpoint_artifact_paths(
                descriptor_path
            )
            expected_result_kind = "ORCHESTRATOR_WORKSTREAM_CONTINUATION"
            expected_descriptor_kind = workstreams.CHECKPOINT_KIND
        else:
            return {
                "source_key": source_key,
                "state": "unsupported",
                "reason": f"no retained-state adapter for {kind}",
            }
        if not descriptor_path.is_file():
            return {
                "source_key": source_key,
                "state": "missing",
                "descriptor_path": str(descriptor_path),
            }
        descriptor = core.load_object(descriptor_path)
        if (
            not core.is_supported_schema_version(descriptor.get("schema_version"))
            or descriptor.get("kind") != expected_descriptor_kind
        ):
            raise ContinuityError("invalid retained descriptor contract")
        if not result_path.is_file():
            return {
                "source_key": source_key,
                "state": "pending",
                "descriptor_path": str(descriptor_path),
                "descriptor_status": descriptor.get("status")
                or descriptor.get("decision"),
            }
        result = core.load_object(result_path)
        if (
            not core.is_supported_schema_version(result.get("schema_version"))
            or result.get("kind") != expected_result_kind
        ):
            raise ContinuityError("invalid retained result contract")
        if kind == "worker":
            terminal_status = result.get("terminal_status")
            if terminal_status not in core.TERMINAL_STATUSES:
                raise ContinuityError("worker result is not terminal")
        elif kind == "workstream":
            if result.get("status") != "ready":
                raise ContinuityError("workstream continuation is not ready")
            terminal_status = "completed"
        else:
            verification_status = result.get("status")
            if verification_status not in {
                "passed",
                "failed",
                "errored",
                "cancelled",
            }:
                raise ContinuityError("verification result is not terminal")
            terminal_status = (
                "completed" if verification_status == "passed" else "failed"
            )
        facts = {
            "result": _artifact_fact(result_path),
            "descriptor": _artifact_fact(descriptor_path),
            "applicability": {
                "source_key": source_key,
                "operation_id": identity,
                "status": "matched_exact_operation",
            },
        }
        if evidence_path.is_file():
            facts["evidence"] = _artifact_fact(evidence_path)
        return {
            "source_key": source_key,
            "state": "terminal",
            "status": terminal_status,
            "data": facts,
        }
    except (OSError, RuntimeError, ValueError) as error:
        return {
            "source_key": source_key,
            "state": "invalid",
            "reason": str(error)[:500],
        }


def _reconcile_sources(
    project_root: Path,
    connection: sqlite3.Connection,
    *,
    sources: list[str],
    state_dir: str,
) -> list[str]:
    recorded: list[str] = []
    for source_key in dict.fromkeys(sources):
        if source_key.startswith("obligation:"):
            continue
        observation = _retained_source_observation(
            project_root, source_key, state_dir=state_dir
        )
        if observation["state"] != "terminal":
            continue
        changed, outcome_id = _record_result(
            connection,
            source_key=source_key,
            status_value=str(observation["status"]),
            event_id=None,
            data=observation["data"],
        )
        if changed:
            recorded.append(outcome_id)
    return recorded


def _record_result(
    connection: sqlite3.Connection,
    *,
    source_key: str,
    status_value: str,
    event_id: str | None,
    data: dict[str, Any],
) -> tuple[bool, str]:
    digest, outcome_id = _outcome_identity(
        source_key, status_value, data, event_id=event_id
    )
    legacy_digest = _legacy_outcome_digest(source_key, status_value, data)
    existing = connection.execute(
        """SELECT outcome_id, event_id FROM results
           WHERE source_key=? AND digest IN (?,?)
           ORDER BY recorded_at LIMIT 1""",
        (source_key, digest, legacy_digest),
    ).fetchone()
    if existing is None:
        candidates = connection.execute(
            """SELECT outcome_id, event_id, data_json FROM results
               WHERE source_key=? AND status=? ORDER BY recorded_at""",
            (source_key, status_value),
        ).fetchall()
        unanchored = [
            row
            for row in candidates
            if not _outcome_has_stable_anchor(
                _decode(row["data_json"], {}), event_id=row["event_id"]
            )
        ]
        if len(candidates) == 1 and len(unanchored) == 1:
            existing = unanchored[0]
    if existing is not None:
        connection.execute(
            """UPDATE results SET digest=?, event_id=COALESCE(event_id, ?), data_json=?
               WHERE outcome_id=?""",
            (digest, event_id, _json(data), existing["outcome_id"]),
        )
        return False, str(existing["outcome_id"])
    connection.execute(
        """INSERT INTO results(
               outcome_id, source_key, status, event_id, digest, data_json, recorded_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            outcome_id,
            source_key,
            status_value,
            event_id,
            digest,
            _json(data),
            core.utc_now(),
        ),
    )
    return True, outcome_id


def _outcome_identity(
    source_key: str,
    status_value: str,
    data: dict[str, Any],
    *,
    event_id: str | None = None,
) -> tuple[str, str]:
    result = data.get("result")
    reference = data.get("result_reference")
    if isinstance(result, dict) and isinstance(result.get("sha256"), str):
        identity: object = {"result_sha256": result["sha256"]}
    elif isinstance(reference, dict):
        identity = {
            key: reference[key]
            for key in ("content_sha256", "reference_sha256", "value")
            if reference.get(key) is not None
        }
    elif event_id:
        identity = {"event_id": event_id}
    else:
        identity = data
    digest = _digest(
        {
            "source_key": source_key,
            "status": status_value,
            "identity": identity,
        }
    )
    return digest, f"result-{digest[:24]}"


def _legacy_outcome_digest(
    source_key: str, status_value: str, data: dict[str, Any]
) -> str:
    return _digest(
        {"source_key": source_key, "status": status_value, "data": data}
    )


def _outcome_has_stable_anchor(
    data: dict[str, Any], *, event_id: str | None
) -> bool:
    result = data.get("result")
    reference = data.get("result_reference")
    return bool(
        (isinstance(result, dict) and isinstance(result.get("sha256"), str))
        or (isinstance(reference, dict) and any(
            reference.get(key) is not None
            for key in ("content_sha256", "reference_sha256", "value")
        ))
        or event_id
    )


def _assignment_wait_satisfied(
    connection: sqlite3.Connection, obligation_id: str
) -> tuple[bool, list[str]]:
    checkpoint_row = connection.execute(
        "SELECT * FROM assignment_checkpoints WHERE obligation_id=?",
        (obligation_id,),
    ).fetchone()
    if checkpoint_row is None or checkpoint_row["mode"] != "waiting":
        return False, []
    sources = _decode(checkpoint_row["sources_json"], [])
    if not sources:
        return False, []
    rows = connection.execute(
        "SELECT outcome_id, source_key FROM results WHERE source_key IN "
        f"({','.join('?' for _ in sources)}) ORDER BY recorded_at, outcome_id",
        tuple(sources),
    ).fetchall()
    ready_sources = {str(row["source_key"]) for row in rows}
    handled = set(_decode(checkpoint_row["handled_json"], []))
    unhandled = [
        str(row["outcome_id"])
        for row in rows
        if row["outcome_id"] not in handled
    ]
    satisfied = (
        bool(unhandled)
        if checkpoint_row["wait_mode"] == "any"
        else set(sources) <= ready_sources and bool(unhandled)
    )
    if not satisfied:
        return False, []
    if checkpoint_row["wait_mode"] == "any":
        return True, unhandled[:1]
    return True, unhandled


def _active_assignment_wait_activation(
    connection: sqlite3.Connection,
    *,
    obligation: sqlite3.Row,
) -> sqlite3.Row | None:
    marker = f"obligation:{obligation['obligation_id']}"
    actor = _require_actor(connection, obligation["assignee_actor"])
    for activation in connection.execute(
        """SELECT * FROM activations
           WHERE work_id=? AND actor_id=? AND reason='assignment_wait_satisfied'
             AND status IN ('pending','published','claimed')
             AND assignment_generation=? AND endpoint_generation=?
           ORDER BY created_at, activation_id""",
        (
            obligation["work_id"],
            obligation["assignee_actor"],
            obligation["generation"],
            actor["generation"],
        ),
    ).fetchall():
        if marker in _decode(activation["manifest_json"], []):
            return activation
    return None


def _evaluate_assignment_wait(
    connection: sqlite3.Connection, obligation_id: str
) -> str | None:
    obligation = connection.execute(
        "SELECT * FROM obligations WHERE obligation_id=?", (obligation_id,)
    ).fetchone()
    checkpoint_row = connection.execute(
        "SELECT * FROM assignment_checkpoints WHERE obligation_id=?",
        (obligation_id,),
    ).fetchone()
    if (
        obligation is None
        or obligation["status"] != "open"
        or checkpoint_row is None
        or int(checkpoint_row["assignment_generation"])
        != int(obligation["generation"])
    ):
        return None
    if not _assignment_continuation_allowed(connection, obligation=obligation):
        return None
    satisfied, manifest_results = _assignment_wait_satisfied(connection, obligation_id)
    if not satisfied:
        return None
    active = _active_assignment_wait_activation(
        connection, obligation=obligation
    )
    if active is not None:
        return str(active["activation_id"])
    return _create_activation(
        connection,
        work=_require_work(connection, obligation["work_id"]),
        actor_id=obligation["assignee_actor"],
        reason="assignment_wait_satisfied",
        manifest=[f"obligation:{obligation_id}", *manifest_results],
        assignment_generation=int(obligation["generation"]),
    )


def _assignment_continuation_allowed(
    connection: sqlite3.Connection, *, obligation: sqlite3.Row
) -> bool:
    work = _require_work(connection, obligation["work_id"])
    if work["mode"] != "complete":
        return True
    return connection.execute(
        "SELECT 1 FROM requests WHERE obligation_id=?",
        (obligation["obligation_id"],),
    ).fetchone() is not None


def _effective_explicit_stop(
    connection: sqlite3.Connection, *, work: sqlite3.Row, actor_id: str
) -> str | None:
    controls = connection.execute(
        """SELECT scope_kind, scope_id, reason FROM recovery_controls
           WHERE state='stopped' AND (
             scope_kind='project' OR
             (scope_kind='actor' AND scope_id=?) OR
             (scope_kind='work' AND scope_id=?))
           ORDER BY CASE scope_kind WHEN 'project' THEN 0 WHEN 'actor' THEN 1 ELSE 2 END
           LIMIT 1""",
        (actor_id, work["work_id"]),
    ).fetchone()
    if controls is None:
        return None
    return f"{controls['scope_kind']}:{controls['scope_id']}:{controls['reason']}"


def _effective_recovery_stop(
    connection: sqlite3.Connection, *, work: sqlite3.Row, actor_id: str
) -> str | None:
    if work["mode"] in {"paused", "complete"}:
        return f"work_{work['mode']}"
    return _effective_explicit_stop(connection, work=work, actor_id=actor_id)


def _revoke_manifest_activations(
    connection: sqlite3.Connection,
    *,
    marker: str,
    reasons: set[str] | None = None,
) -> None:
    query = "SELECT activation_id, reason, manifest_json FROM activations " \
        "WHERE status IN ('pending','published')"
    for activation in connection.execute(query).fetchall():
        if reasons is not None and activation["reason"] not in reasons:
            continue
        if marker not in _decode(activation["manifest_json"], []):
            continue
        connection.execute(
            "UPDATE activations SET status='revoked' WHERE activation_id=?",
            (activation["activation_id"],),
        )


def _resolve_recovery_incidents_for_manifest(
    connection: sqlite3.Connection, *, marker: str, cause: str
) -> None:
    now = core.utc_now()
    for incident in connection.execute(
        "SELECT incident_id, activation_id FROM recovery_incidents "
        "WHERE cause=? AND status!='resolved'",
        (cause,),
    ).fetchall():
        if not incident["activation_id"]:
            continue
        activation = connection.execute(
            "SELECT manifest_json FROM activations WHERE activation_id=?",
            (incident["activation_id"],),
        ).fetchone()
        if activation is None or marker not in _decode(
            activation["manifest_json"], []
        ):
            continue
        connection.execute(
            "UPDATE recovery_incidents SET status='resolved', updated_at=? "
            "WHERE incident_id=?",
            (now, incident["incident_id"]),
        )


def _revoke_owner_control_activations(
    connection: sqlite3.Connection, *, work_id: str, actor_id: str
) -> None:
    for activation in connection.execute(
        """SELECT activation_id, reason, manifest_json FROM activations
           WHERE work_id=? AND actor_id=? AND status IN ('pending','published')
             AND reason IN (
               'continue_checkpoint','wait_satisfied','ownership_transferred',
               'work_recovery'
             )""",
        (work_id, actor_id),
    ).fetchall():
        if activation["reason"] == "work_recovery" and any(
            value.startswith("request:")
            for value in _decode(activation["manifest_json"], [])
        ):
            continue
        connection.execute(
            "UPDATE activations SET status='revoked' WHERE activation_id=?",
            (activation["activation_id"],),
        )


def _revoke_product_activations_on_complete(
    connection: sqlite3.Connection, *, work_id: str
) -> None:
    """Fence product work while preserving already-created communication."""

    for activation in connection.execute(
        """SELECT activation_id, reason, manifest_json FROM activations
           WHERE work_id=? AND status IN ('pending','published')""",
        (work_id,),
    ).fetchall():
        manifest = _decode(activation["manifest_json"], [])
        communication = activation["reason"] in {"request_message", "request_reply"}
        communication = communication or any(
            value.startswith("request:") for value in manifest
        )
        if not communication:
            obligation_id = _manifest_value(manifest, "obligation:")
            if obligation_id is not None:
                communication = connection.execute(
                    "SELECT 1 FROM requests WHERE obligation_id=?",
                    (obligation_id,),
                ).fetchone() is not None
        if communication:
            continue
        connection.execute(
            "UPDATE activations SET status='revoked' WHERE activation_id=?",
            (activation["activation_id"],),
        )


def _active_current_activation(
    connection: sqlite3.Connection,
    *,
    activation_id: str | None,
    actor_id: str,
    reason: str,
) -> sqlite3.Row | None:
    if activation_id is None:
        return None
    actor = _require_actor(connection, actor_id)
    return connection.execute(
        """SELECT * FROM activations
           WHERE activation_id=? AND actor_id=? AND reason=?
             AND status IN ('pending','published','claimed')
             AND endpoint_generation=?""",
        (activation_id, actor_id, reason, actor["generation"]),
    ).fetchone()


def _active_obligation_activation(
    connection: sqlite3.Connection,
    *,
    obligation: sqlite3.Row,
    reasons: set[str],
) -> sqlite3.Row | None:
    marker = f"obligation:{obligation['obligation_id']}"
    actor = _require_actor(connection, obligation["assignee_actor"])
    for activation in connection.execute(
        """SELECT * FROM activations
           WHERE work_id=? AND actor_id=?
             AND status IN ('pending','published','claimed')
             AND assignment_generation=? AND endpoint_generation=?
           ORDER BY created_at DESC, activation_id DESC""",
        (
            obligation["work_id"],
            obligation["assignee_actor"],
            obligation["generation"],
            actor["generation"],
        ),
    ).fetchall():
        if activation["reason"] in reasons and marker in _decode(
            activation["manifest_json"], []
        ):
            return activation
    return None


def _current_obligation_claim(
    connection: sqlite3.Connection, *, obligation: sqlite3.Row
) -> sqlite3.Row | None:
    activation_id = obligation["claimed_activation_id"]
    if (
        activation_id is None
        or int(obligation["claimed_generation"] or 0)
        != int(obligation["generation"])
    ):
        return None
    actor = _require_actor(connection, obligation["assignee_actor"])
    return connection.execute(
        """SELECT * FROM activations
           WHERE activation_id=? AND actor_id=? AND status='claimed'
             AND assignment_generation=? AND endpoint_generation=?""",
        (
            activation_id,
            obligation["assignee_actor"],
            obligation["generation"],
            actor["generation"],
        ),
    ).fetchone()


def _rearm_obligation_route(
    connection: sqlite3.Connection,
    *,
    work: sqlite3.Row,
    obligation: sqlite3.Row,
    request: sqlite3.Row | None,
    assignment: sqlite3.Row | None,
) -> str | None:
    """Restore one current-endpoint route without reviving completed product work."""

    actor_id = str(obligation["assignee_actor"])
    if _effective_explicit_stop(
        connection, work=work, actor_id=actor_id
    ) is not None:
        return None
    if work["mode"] == "complete" and request is None:
        return None
    marker = f"obligation:{obligation['obligation_id']}"
    request_marker = (
        [f"request:{request['request_id']}"] if request is not None else []
    )
    if assignment is not None:
        mode = str(assignment["mode"])
        if mode == "waiting":
            return _evaluate_assignment_wait(connection, obligation["obligation_id"])
        if mode == "paused":
            current_claim = _current_obligation_claim(
                connection, obligation=obligation
            )
            if current_claim is not None:
                return str(current_claim["activation_id"])
        reason = (
            "assignment_continue"
            if mode == "continue"
            else "assignment_paused_control"
        )
        active = _active_obligation_activation(
            connection, obligation=obligation, reasons={reason}
        )
        if active is not None:
            return str(active["activation_id"])
        return _create_activation(
            connection,
            work=work,
            actor_id=actor_id,
            reason=reason,
            manifest=[*request_marker, marker],
            assignment_generation=int(obligation["generation"]),
        )
    if request is not None:
        if request["terminal_response_id"] is not None:
            return None
        active = _active_current_activation(
            connection,
            activation_id=request["request_activation_id"],
            actor_id=actor_id,
            reason="request_message",
        )
        if active is not None and int(active["assignment_generation"] or 0) == int(
            obligation["generation"]
        ):
            return str(active["activation_id"])
        activation_id = _create_activation(
            connection,
            work=work,
            actor_id=actor_id,
            reason="request_message",
            manifest=[*request_marker, marker],
            assignment_generation=int(obligation["generation"]),
        )
        connection.execute(
            """UPDATE requests SET status='delivery_pending',
                   request_activation_id=?, updated_at=? WHERE request_id=?""",
            (activation_id, core.utc_now(), request["request_id"]),
        )
        return activation_id
    if obligation["claimed_at"] is not None:
        return None
    return _schedule_obligation_activations(
        connection,
        work=work,
        actor_id=actor_id,
        obligation=obligation,
    )


def _reconcile_request_deliveries(
    connection: sqlite3.Connection, *, work_id: str | None = None
) -> list[str]:
    query = (
        "SELECT * FROM requests "
        "WHERE status IN ('delivery_pending','claimed','reply_ready')"
    )
    arguments: tuple[Any, ...] = ()
    if work_id is not None:
        query += " AND work_id=?"
        arguments = (work_id,)
    repaired: list[str] = []
    for request in connection.execute(query, arguments).fetchall():
        work = _require_work(connection, request["work_id"])
        if request["status"] in {"delivery_pending", "claimed"}:
            actor_id = str(request["recipient_actor"])
            pointer = request["request_activation_id"]
            reason = "request_message"
        else:
            actor_id = str(request["return_actor"])
            pointer = request["reply_activation_id"]
            reason = "request_reply"
        if _effective_explicit_stop(
            connection, work=work, actor_id=actor_id
        ) is not None:
            continue
        if _active_current_activation(
            connection,
            activation_id=pointer,
            actor_id=actor_id,
            reason=reason,
        ) is not None:
            continue
        if reason == "request_message" and request["obligation_id"] is not None:
            assignment = connection.execute(
                "SELECT 1 FROM assignment_checkpoints WHERE obligation_id=?",
                (request["obligation_id"],),
            ).fetchone()
            if assignment is not None:
                continue
        manifest = [f"request:{request['request_id']}"]
        assignment_generation = None
        if reason == "request_message" and request["obligation_id"] is not None:
            obligation = connection.execute(
                "SELECT generation FROM obligations WHERE obligation_id=?",
                (request["obligation_id"],),
            ).fetchone()
            if obligation is None:
                continue
            manifest.append(f"obligation:{request['obligation_id']}")
            assignment_generation = int(obligation["generation"])
        elif reason == "request_reply":
            if request["terminal_response_id"] is None:
                continue
            manifest.append(f"response:{request['terminal_response_id']}")
            result = connection.execute(
                "SELECT outcome_id FROM results WHERE source_key=? "
                "ORDER BY recorded_at DESC, outcome_id DESC LIMIT 1",
                (f"obligation:{request['obligation_id']}",),
            ).fetchone()
            if result is not None:
                manifest.append(str(result["outcome_id"]))
        activation_id = _create_activation(
            connection,
            work=work,
            actor_id=actor_id,
            reason=reason,
            manifest=manifest,
            assignment_generation=assignment_generation,
        )
        pointer_column = (
            "request_activation_id"
            if reason == "request_message"
            else "reply_activation_id"
        )
        connection.execute(
            f"UPDATE requests SET {pointer_column}=?, "
            + ("status='delivery_pending', " if reason == "request_message" else "")
            + "updated_at=? "
            "WHERE request_id=?",
            (activation_id, core.utc_now(), request["request_id"]),
        )
        repaired.append(activation_id)
    return repaired


def _rearm_scope_routes(
    connection: sqlite3.Connection, *, scope_kind: str, scope_id: str
) -> None:
    if scope_kind == "work":
        works = connection.execute(
            "SELECT * FROM works WHERE work_id=?", (scope_id,)
        ).fetchall()
    elif scope_kind == "actor":
        works = connection.execute(
            "SELECT DISTINCT w.* FROM works w "
            "LEFT JOIN obligations o ON o.work_id=w.work_id "
            "LEFT JOIN requests r ON r.work_id=w.work_id "
            "WHERE w.owner_actor=? OR o.assignee_actor=? "
            "OR r.recipient_actor=? OR r.return_actor=?",
            (scope_id, scope_id, scope_id, scope_id),
        ).fetchall()
    else:
        works = connection.execute("SELECT * FROM works").fetchall()
    for work in works:
        _reconcile_request_deliveries(connection, work_id=work["work_id"])
        owner = str(work["owner_actor"])
        if work["mode"] != "complete" and _effective_explicit_stop(
            connection, work=work, actor_id=owner
        ) is None:
            if work["mode"] == "continue":
                owner_endpoint_generation = int(
                    _require_actor(connection, owner)["generation"]
                )
                active = connection.execute(
                    """SELECT 1 FROM activations
                       WHERE work_id=? AND actor_id=?
                         AND reason='continue_checkpoint'
                         AND status IN ('pending','published','claimed')
                         AND work_revision=? AND control_epoch=?
                         AND endpoint_generation=?""",
                    (
                        work["work_id"],
                        owner,
                        work["revision"],
                        work["control_epoch"],
                        owner_endpoint_generation,
                    ),
                ).fetchone()
                if active is None:
                    _create_activation(
                        connection,
                        work=work,
                        actor_id=owner,
                        reason="continue_checkpoint",
                        manifest=[],
                    )
            elif work["mode"] == "waiting":
                _evaluate_wait(connection, work["work_id"])
        for obligation in connection.execute(
            "SELECT * FROM obligations WHERE work_id=? AND status='open'",
            (work["work_id"],),
        ).fetchall():
            assignee = str(obligation["assignee_actor"])
            if _effective_explicit_stop(
                connection, work=work, actor_id=assignee
            ) is not None:
                continue
            assignment = connection.execute(
                "SELECT * FROM assignment_checkpoints WHERE obligation_id=?",
                (obligation["obligation_id"],),
            ).fetchone()
            linked_request = connection.execute(
                "SELECT * FROM requests WHERE obligation_id=?",
                (obligation["obligation_id"],),
            ).fetchone()
            _rearm_obligation_route(
                connection,
                work=work,
                obligation=obligation,
                request=linked_request,
                assignment=assignment,
            )


def _reconcile_recovery(connection: sqlite3.Connection) -> dict[str, Any]:
    now = datetime.now(UTC)
    queued: list[str] = []
    suppressed: list[dict[str, str]] = []
    select_sql = """SELECT o.*, p.interval_seconds, p.max_interval_seconds,
                  w.mode AS work_mode, w.revision AS current_work_revision,
                  w.control_epoch AS current_control_epoch,
                  w.owner_actor, a.endpoint_json, a.generation AS actor_generation
           FROM obligations o
           JOIN recovery_policies p ON p.work_id=o.work_id AND p.armed=1
           JOIN works w ON w.work_id=o.work_id
           JOIN actors a ON a.actor_id=o.assignee_actor AND a.active=1
           WHERE o.status='open' AND o.claimed_at IS NOT NULL"""
    total_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM obligations o "
            "JOIN recovery_policies p ON p.work_id=o.work_id AND p.armed=1 "
            "JOIN actors a ON a.actor_id=o.assignee_actor AND a.active=1 "
            "WHERE o.status='open' AND o.claimed_at IS NOT NULL"
        ).fetchone()[0]
    )
    cursor_row = connection.execute(
        "SELECT value FROM metadata WHERE key='recovery_cursor'"
    ).fetchone()
    cursor = str(cursor_row["value"] if cursor_row is not None else "")
    rows = connection.execute(
        select_sql + " AND o.obligation_id>? ORDER BY o.obligation_id LIMIT ?",
        (cursor, MAX_RECOVERY_BATCH),
    ).fetchall()
    if not rows and cursor:
        cursor = ""
        rows = connection.execute(
            select_sql + " ORDER BY o.obligation_id LIMIT ?",
            (MAX_RECOVERY_BATCH,),
        ).fetchall()
    next_cursor = str(rows[-1]["obligation_id"]) if rows else cursor
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('recovery_cursor', ?)",
        (next_cursor,),
    )
    coverage_complete = total_count <= MAX_RECOVERY_BATCH
    for obligation in rows:
        work = _require_work(connection, obligation["work_id"])
        request = connection.execute(
            "SELECT * FROM requests WHERE obligation_id=?",
            (obligation["obligation_id"],),
        ).fetchone()
        stopped = (
            _effective_explicit_stop(
                connection, work=work, actor_id=obligation["assignee_actor"]
            )
            if request is not None
            else _effective_recovery_stop(
                connection, work=work, actor_id=obligation["assignee_actor"]
            )
        )
        if stopped:
            suppressed.append(
                {"obligation_id": obligation["obligation_id"], "reason": stopped}
            )
            continue
        checkpoint_row = connection.execute(
            "SELECT * FROM assignment_checkpoints WHERE obligation_id=?",
            (obligation["obligation_id"],),
        ).fetchone()
        if (
            checkpoint_row is not None
            and int(checkpoint_row["assignment_generation"])
            == int(obligation["generation"])
            and checkpoint_row["mode"] == "paused"
        ):
            suppressed.append(
                {
                    "obligation_id": obligation["obligation_id"],
                    "reason": "assignment_paused",
                }
            )
            continue
        if (
            checkpoint_row is not None
            and int(checkpoint_row["assignment_generation"])
            == int(obligation["generation"])
            and checkpoint_row["mode"] == "waiting"
        ):
            _evaluate_assignment_wait(connection, obligation["obligation_id"])
            satisfied, _manifest = _assignment_wait_satisfied(
                connection, obligation["obligation_id"]
            )
            claimed = connection.execute(
                "SELECT reason FROM activations WHERE activation_id=? "
                "AND status='claimed'",
                (obligation["claimed_activation_id"],),
            ).fetchone()
            if (
                not satisfied
                or claimed is None
                or claimed["reason"] != "assignment_wait_satisfied"
            ):
                suppressed.append(
                    {
                        "obligation_id": obligation["obligation_id"],
                        "reason": "assignment_waiting",
                    }
                )
                continue
        transition = str(
            obligation["claimed_at"]
            or (
                checkpoint_row["updated_at"]
                if checkpoint_row is not None
                else request["updated_at"]
                if request is not None
                else ""
            )
        )
        try:
            due = datetime.fromisoformat(
                transition.replace("Z", "+00:00")
            ) + timedelta(seconds=float(obligation["interval_seconds"]))
            due_is_future = due > now
        except (TypeError, ValueError):
            suppressed.append(
                {
                    "obligation_id": obligation["obligation_id"],
                    "reason": "invalid_recovery_timestamp",
                }
            )
            continue
        if due_is_future:
            continue
        endpoint = _decode(obligation["endpoint_json"], {})
        try:
            capability = host_capabilities.for_host(str(endpoint["host"]))
        except (KeyError, ValueError):
            suppressed.append(
                {
                    "obligation_id": obligation["obligation_id"],
                    "reason": "host_capability_unknown",
                }
            )
            continue
        capability_level = str(capability["sequential_queue_processing"])
        safe = capability_level == "supported" or capability.get(
            "terminal_turn_observation"
        ) == "supported"
        cause = "unconfirmed_checkpoint_or_response"
        identity = {
            "work_id": obligation["work_id"],
            "obligation_id": obligation["obligation_id"],
            "generation": obligation["generation"],
            "endpoint_generation": obligation["actor_generation"],
            "transition": transition,
            "cause": cause,
        }
        incident_id = f"recovery-{_digest(identity)[:24]}"
        incident = connection.execute(
            "SELECT * FROM recovery_incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if incident is not None:
            if incident["status"] == "queued":
                continue
            try:
                next_due = datetime.fromisoformat(
                    str(incident["next_inspection_at"]).replace("Z", "+00:00")
                )
                waiting_not_due = incident["status"] == "waiting" and next_due > now
            except (TypeError, ValueError):
                connection.execute(
                    "UPDATE recovery_incidents SET status='invalid', updated_at=? "
                    "WHERE incident_id=?",
                    (core.utc_now(), incident["incident_id"]),
                )
                suppressed.append(
                    {
                        "obligation_id": obligation["obligation_id"],
                        "reason": "invalid_incident_timestamp",
                    }
                )
                continue
            if waiting_not_due:
                continue
        if not safe:
            connection.execute(
                """INSERT INTO recovery_incidents(
                       incident_id, work_id, obligation_id, actor_id, cause,
                       work_revision, control_epoch, assignment_generation,
                       endpoint_generation, capability_level, status, attempts,
                       next_inspection_at, activation_id, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,'capability_blocked',0,?,NULL,?,?)
                   ON CONFLICT(incident_id) DO UPDATE SET
                     status='capability_blocked',
                     capability_level=excluded.capability_level,
                     next_inspection_at=excluded.next_inspection_at,
                     updated_at=excluded.updated_at""",
                (
                    incident_id,
                    obligation["work_id"],
                    obligation["obligation_id"],
                    obligation["assignee_actor"],
                    cause,
                    obligation["current_work_revision"],
                    obligation["current_control_epoch"],
                    obligation["generation"],
                    obligation["actor_generation"],
                    capability_level,
                    due.isoformat(timespec="milliseconds"),
                    core.utc_now(),
                    core.utc_now(),
                ),
            )
            suppressed.append(
                {
                    "obligation_id": obligation["obligation_id"],
                    "reason": "capability_blocked",
                }
            )
            continue
        manifest = [f"obligation:{obligation['obligation_id']}"]
        if request is not None:
            manifest.insert(0, f"request:{request['request_id']}")
        activation_id = _create_activation(
            connection,
            work=work,
            actor_id=obligation["assignee_actor"],
            reason="assignment_recovery",
            manifest=manifest,
            assignment_generation=int(obligation["generation"]),
        )
        attempts = int(incident["attempts"]) if incident is not None else 0
        connection.execute(
            """INSERT INTO recovery_incidents(
                   incident_id, work_id, obligation_id, actor_id, cause,
                   work_revision, control_epoch, assignment_generation,
                   endpoint_generation, capability_level, status, attempts,
                   next_inspection_at, activation_id, created_at, updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,?, ?,?)
               ON CONFLICT(incident_id) DO UPDATE SET
                 status='queued', activation_id=excluded.activation_id,
                 next_inspection_at=excluded.next_inspection_at,
                 updated_at=excluded.updated_at""",
            (
                incident_id,
                obligation["work_id"],
                obligation["obligation_id"],
                obligation["assignee_actor"],
                cause,
                obligation["current_work_revision"],
                obligation["current_control_epoch"],
                obligation["generation"],
                obligation["actor_generation"],
                capability_level,
                attempts,
                due.isoformat(timespec="milliseconds"),
                activation_id,
                core.utc_now(),
                core.utc_now(),
            ),
        )
        queued.append(activation_id)
    reply_select = """SELECT r.*, p.interval_seconds, p.max_interval_seconds,
                             w.mode AS work_mode,
                             w.revision AS current_work_revision,
                             w.control_epoch AS current_control_epoch,
                             a.endpoint_json,
                             a.generation AS actor_generation,
                             x.claimed_at AS source_claimed_at
                      FROM requests r
                      JOIN recovery_policies p
                        ON p.work_id=r.work_id AND p.armed=1
                      JOIN works w ON w.work_id=r.work_id
                      JOIN actors a
                        ON a.actor_id=r.return_actor AND a.active=1
                      JOIN activations x
                        ON x.activation_id=r.reply_activation_id
                       AND x.status='claimed'
                      WHERE r.status='reply_ready'"""
    reply_total = int(
        connection.execute(f"SELECT COUNT(*) FROM ({reply_select})").fetchone()[0]
    )
    reply_cursor_row = connection.execute(
        "SELECT value FROM metadata WHERE key='recovery_reply_cursor'"
    ).fetchone()
    reply_cursor = str(reply_cursor_row["value"] if reply_cursor_row else "")
    reply_rows = connection.execute(
        reply_select + " AND r.request_id>? ORDER BY r.request_id LIMIT ?",
        (reply_cursor, MAX_RECOVERY_BATCH),
    ).fetchall()
    if not reply_rows and reply_cursor:
        reply_cursor = ""
        reply_rows = connection.execute(
            reply_select + " ORDER BY r.request_id LIMIT ?", (MAX_RECOVERY_BATCH,)
        ).fetchall()
    next_reply_cursor = (
        str(reply_rows[-1]["request_id"]) if reply_rows else reply_cursor
    )
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) "
        "VALUES('recovery_reply_cursor', ?)",
        (next_reply_cursor,),
    )
    for request in reply_rows:
        work = _require_work(connection, request["work_id"])
        stopped = _effective_explicit_stop(
            connection, work=work, actor_id=request["return_actor"]
        )
        if stopped:
            suppressed.append(
                {"request_id": request["request_id"], "reason": stopped}
            )
            continue
        try:
            due = datetime.fromisoformat(
                str(request["source_claimed_at"]).replace("Z", "+00:00")
            ) + timedelta(seconds=float(request["interval_seconds"]))
            due_is_future = due > now
        except (TypeError, ValueError):
            suppressed.append(
                {
                    "request_id": request["request_id"],
                    "reason": "invalid_recovery_timestamp",
                }
            )
            continue
        if due_is_future:
            continue
        endpoint = _decode(request["endpoint_json"], {})
        try:
            capability = host_capabilities.for_host(str(endpoint["host"]))
        except (KeyError, ValueError):
            suppressed.append(
                {
                    "request_id": request["request_id"],
                    "reason": "host_capability_unknown",
                }
            )
            continue
        capability_level = str(capability["sequential_queue_processing"])
        safe = capability_level == "supported" or capability.get(
            "terminal_turn_observation"
        ) == "supported"
        cause = "unconfirmed_reply_handling"
        identity = {
            "work_id": request["work_id"],
            "request_id": request["request_id"],
            "endpoint_generation": request["actor_generation"],
            "source_activation_id": request["reply_activation_id"],
            "cause": cause,
        }
        incident_id = f"recovery-{_digest(identity)[:24]}"
        incident = connection.execute(
            "SELECT * FROM recovery_incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if incident is not None:
            if incident["status"] == "queued":
                continue
            try:
                next_due = datetime.fromisoformat(
                    str(incident["next_inspection_at"]).replace("Z", "+00:00")
                )
                waiting_not_due = (
                    incident["status"] == "waiting" and next_due > now
                )
            except (TypeError, ValueError):
                connection.execute(
                    "UPDATE recovery_incidents SET status='invalid', updated_at=? "
                    "WHERE incident_id=?",
                    (core.utc_now(), incident["incident_id"]),
                )
                suppressed.append(
                    {
                        "request_id": request["request_id"],
                        "reason": "invalid_incident_timestamp",
                    }
                )
                continue
            if waiting_not_due:
                continue
        if not safe:
            connection.execute(
                """INSERT INTO recovery_incidents(
                       incident_id, work_id, obligation_id, actor_id, cause,
                       work_revision, control_epoch, assignment_generation,
                       endpoint_generation, capability_level, status, attempts,
                       next_inspection_at, activation_id, created_at, updated_at
                   ) VALUES(?,?,NULL,?,?,?,?,NULL,?,?,'capability_blocked',0,
                            ?,NULL,?,?)
                   ON CONFLICT(incident_id) DO UPDATE SET
                     status='capability_blocked',
                     capability_level=excluded.capability_level,
                     next_inspection_at=excluded.next_inspection_at,
                     updated_at=excluded.updated_at""",
                (
                    incident_id,
                    request["work_id"],
                    request["return_actor"],
                    cause,
                    request["current_work_revision"],
                    request["current_control_epoch"],
                    request["actor_generation"],
                    capability_level,
                    due.isoformat(timespec="milliseconds"),
                    core.utc_now(),
                    core.utc_now(),
                ),
            )
            suppressed.append(
                {
                    "request_id": request["request_id"],
                    "reason": "capability_blocked",
                }
            )
            continue
        activation_id = _create_activation(
            connection,
            work=work,
            actor_id=request["return_actor"],
            reason="work_recovery",
            manifest=[
                f"request:{request['request_id']}",
                f"activation:{request['reply_activation_id']}",
            ],
        )
        attempts = int(incident["attempts"]) if incident is not None else 0
        connection.execute(
            """INSERT INTO recovery_incidents(
                   incident_id, work_id, obligation_id, actor_id, cause,
                   work_revision, control_epoch, assignment_generation,
                   endpoint_generation, capability_level, status, attempts,
                   next_inspection_at, activation_id, created_at, updated_at
               ) VALUES(?,?,NULL,?,?,?,?,NULL,?,?,'queued',?,?,?,?,?)
               ON CONFLICT(incident_id) DO UPDATE SET
                 status='queued', activation_id=excluded.activation_id,
                 next_inspection_at=excluded.next_inspection_at,
                 updated_at=excluded.updated_at""",
            (
                incident_id,
                request["work_id"],
                request["return_actor"],
                cause,
                request["current_work_revision"],
                request["current_control_epoch"],
                request["actor_generation"],
                capability_level,
                attempts,
                due.isoformat(timespec="milliseconds"),
                activation_id,
                core.utc_now(),
                core.utc_now(),
            ),
        )
        queued.append(activation_id)
    work_select = """SELECT w.*, p.interval_seconds, p.max_interval_seconds,
                            a.endpoint_json,
                            a.generation AS actor_generation,
                            x.activation_id AS source_activation_id,
                            x.reason AS source_reason,
                            x.claimed_at AS source_claimed_at,
                            x.manifest_json AS source_manifest_json
                     FROM works w
                     JOIN recovery_policies p ON p.work_id=w.work_id AND p.armed=1
                     JOIN actors a ON a.actor_id=w.owner_actor AND a.active=1
                     JOIN activations x ON x.activation_id=(
                       SELECT activation_id FROM activations candidate
                       WHERE candidate.work_id=w.work_id
                         AND candidate.actor_id=w.owner_actor
                         AND candidate.status='claimed'
                         AND candidate.work_revision=w.revision
                         AND candidate.control_epoch=w.control_epoch
                         AND candidate.endpoint_generation=a.generation
                         AND candidate.reason IN (
                           'continue_checkpoint','wait_satisfied',
                           'ownership_transferred')
                       ORDER BY candidate.claimed_at DESC,
                                candidate.activation_id DESC LIMIT 1)
                     WHERE w.mode IN ('continue','waiting')"""
    work_total = int(
        connection.execute(f"SELECT COUNT(*) FROM ({work_select})").fetchone()[0]
    )
    work_cursor_row = connection.execute(
        "SELECT value FROM metadata WHERE key='recovery_work_cursor'"
    ).fetchone()
    work_cursor = str(work_cursor_row["value"] if work_cursor_row else "")
    work_rows = connection.execute(
        work_select + " AND w.work_id>? ORDER BY w.work_id LIMIT ?",
        (work_cursor, MAX_RECOVERY_BATCH),
    ).fetchall()
    if not work_rows and work_cursor:
        work_cursor = ""
        work_rows = connection.execute(
            work_select + " ORDER BY w.work_id LIMIT ?", (MAX_RECOVERY_BATCH,)
        ).fetchall()
    next_work_cursor = str(work_rows[-1]["work_id"]) if work_rows else work_cursor
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) "
        "VALUES('recovery_work_cursor', ?)",
        (next_work_cursor,),
    )
    for work_row in work_rows:
        stopped = _effective_recovery_stop(
            connection, work=work_row, actor_id=work_row["owner_actor"]
        )
        if stopped:
            suppressed.append({"work_id": work_row["work_id"], "reason": stopped})
            continue
        try:
            due = datetime.fromisoformat(
                str(work_row["source_claimed_at"]).replace("Z", "+00:00")
            ) + timedelta(seconds=float(work_row["interval_seconds"]))
            due_is_future = due > now
        except (TypeError, ValueError):
            suppressed.append(
                {
                    "work_id": work_row["work_id"],
                    "reason": "invalid_recovery_timestamp",
                }
            )
            continue
        if due_is_future:
            continue
        endpoint = _decode(work_row["endpoint_json"], {})
        try:
            capability = host_capabilities.for_host(str(endpoint["host"]))
        except (KeyError, ValueError):
            suppressed.append(
                {"work_id": work_row["work_id"], "reason": "host_capability_unknown"}
            )
            continue
        capability_level = str(capability["sequential_queue_processing"])
        safe = capability_level == "supported" or capability.get(
            "terminal_turn_observation"
        ) == "supported"
        cause = "unconfirmed_owner_checkpoint"
        identity = {
            "work_id": work_row["work_id"],
            "revision": work_row["revision"],
            "control_epoch": work_row["control_epoch"],
            "endpoint_generation": work_row["actor_generation"],
            "source_activation_id": work_row["source_activation_id"],
            "cause": cause,
        }
        incident_id = f"recovery-{_digest(identity)[:24]}"
        incident = connection.execute(
            "SELECT * FROM recovery_incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if incident is not None:
            if incident["status"] == "queued":
                continue
            try:
                next_due = datetime.fromisoformat(
                    str(incident["next_inspection_at"]).replace("Z", "+00:00")
                )
                waiting_not_due = incident["status"] == "waiting" and next_due > now
            except (TypeError, ValueError):
                connection.execute(
                    "UPDATE recovery_incidents SET status='invalid', updated_at=? "
                    "WHERE incident_id=?",
                    (core.utc_now(), incident["incident_id"]),
                )
                suppressed.append(
                    {
                        "work_id": work_row["work_id"],
                        "reason": "invalid_incident_timestamp",
                    }
                )
                continue
            if waiting_not_due:
                continue
        if not safe:
            suppressed.append(
                {"work_id": work_row["work_id"], "reason": "capability_blocked"}
            )
            connection.execute(
                """INSERT INTO recovery_incidents(
                       incident_id, work_id, obligation_id, actor_id, cause,
                       work_revision, control_epoch, assignment_generation,
                       endpoint_generation, capability_level, status, attempts,
                       next_inspection_at, activation_id, created_at, updated_at
                   ) VALUES(?,?,NULL,?,?,?,?,NULL,?,?,'capability_blocked',0,
                            ?,NULL,?,?)
                   ON CONFLICT(incident_id) DO UPDATE SET
                     status='capability_blocked',
                     capability_level=excluded.capability_level,
                     next_inspection_at=excluded.next_inspection_at,
                     updated_at=excluded.updated_at""",
                (
                    incident_id,
                    work_row["work_id"],
                    work_row["owner_actor"],
                    cause,
                    work_row["revision"],
                    work_row["control_epoch"],
                    work_row["actor_generation"],
                    capability_level,
                    due.isoformat(timespec="milliseconds"),
                    core.utc_now(),
                    core.utc_now(),
                ),
            )
            continue
        activation_id = _create_activation(
            connection,
            work=work_row,
            actor_id=work_row["owner_actor"],
            reason="work_recovery",
            manifest=[f"activation:{work_row['source_activation_id']}"],
        )
        attempts = int(incident["attempts"]) if incident is not None else 0
        connection.execute(
            """INSERT INTO recovery_incidents(
                   incident_id, work_id, obligation_id, actor_id, cause,
                   work_revision, control_epoch, assignment_generation,
                   endpoint_generation, capability_level, status, attempts,
                   next_inspection_at, activation_id, created_at, updated_at
               ) VALUES(?,?,NULL,?,?,?,?,NULL,?,?,'queued',?,?,?,?,?)
               ON CONFLICT(incident_id) DO UPDATE SET
                 status='queued', activation_id=excluded.activation_id,
                 next_inspection_at=excluded.next_inspection_at,
                 updated_at=excluded.updated_at""",
            (
                incident_id,
                work_row["work_id"],
                work_row["owner_actor"],
                cause,
                work_row["revision"],
                work_row["control_epoch"],
                work_row["actor_generation"],
                capability_level,
                attempts,
                due.isoformat(timespec="milliseconds"),
                activation_id,
                core.utc_now(),
                core.utc_now(),
            ),
        )
        queued.append(activation_id)
    return {
        "queued": queued,
        "suppressed": suppressed,
        "coverage_complete": (
            coverage_complete
            and reply_total <= MAX_RECOVERY_BATCH
            and work_total <= MAX_RECOVERY_BATCH
        ),
        "inspected_count": len(rows) + len(reply_rows) + len(work_rows),
        "eligible_count": total_count + reply_total + work_total,
        "next_cursor": next_cursor,
        "next_reply_cursor": next_reply_cursor,
        "next_work_cursor": next_work_cursor,
    }


def _wait_satisfied(connection: sqlite3.Connection, work_id: str) -> bool:
    wait = connection.execute(
        "SELECT * FROM waits WHERE work_id=?", (work_id,)
    ).fetchone()
    if wait is None:
        return False
    sources = _decode(wait["sources_json"], [])
    if not sources:
        return False
    rows = connection.execute(
        "SELECT outcome_id, source_key FROM results WHERE source_key IN "
        f"({','.join('?' for _ in sources)})",
        tuple(sources),
    ).fetchall()
    ready_sources = {row["source_key"] for row in rows}
    handled = set(_decode(wait["handled_json"], []))
    unhandled_sources = {
        row["source_key"] for row in rows if row["outcome_id"] not in handled
    }
    return (
        bool(unhandled_sources)
        if wait["mode"] == "any"
        else set(sources) <= ready_sources and bool(unhandled_sources)
    )


def _evaluate_wait(connection: sqlite3.Connection, work_id: str) -> str | None:
    if not _wait_satisfied(connection, work_id):
        return None
    work = _require_work(connection, work_id)
    if work["mode"] != "waiting":
        return None
    wait = connection.execute(
        "SELECT * FROM waits WHERE work_id=?", (work_id,)
    ).fetchone()
    sources = _decode(wait["sources_json"], [])
    handled = set(_decode(wait["handled_json"], []))
    available_rows = [
        row
        for row in connection.execute(
            "SELECT outcome_id, source_key FROM results WHERE source_key IN "
            f"({','.join('?' for _ in sources)}) "
            "ORDER BY recorded_at, source_key, outcome_id",
            tuple(sources),
        ).fetchall()
        if row["outcome_id"] not in handled
    ]
    available: list[str] = []
    included_sources: set[str] = set()
    for row in available_rows:
        if row["source_key"] in included_sources:
            continue
        available.append(str(row["outcome_id"]))
        included_sources.add(str(row["source_key"]))
    manifest = available[:1] if wait["mode"] == "any" else available
    active = connection.execute(
        """SELECT activation_id FROM activations
           WHERE work_id=? AND actor_id=? AND reason='wait_satisfied'
             AND status IN ('pending','published','claimed')
             AND work_revision=? AND control_epoch=?
             AND endpoint_generation=?""",
        (
            work_id,
            work["owner_actor"],
            work["revision"],
            work["control_epoch"],
            _require_actor(connection, work["owner_actor"])["generation"],
        ),
    ).fetchone()
    if active is not None:
        return active["activation_id"]
    return _create_activation(
        connection,
        work=work,
        actor_id=work["owner_actor"],
        reason="wait_satisfied",
        manifest=manifest,
    )


def _create_activation(
    connection: sqlite3.Connection,
    *,
    work: sqlite3.Row,
    actor_id: str,
    reason: str,
    manifest: list[str],
    not_before: str | None = None,
    assignment_generation: int | None = None,
) -> str:
    actor = _require_actor(connection, actor_id)
    endpoint = _decode(actor["endpoint_json"], {})
    wake_target = binding.wake_target_from_binding(endpoint)
    sequence_row = connection.execute(
        "SELECT value FROM metadata WHERE key='activation_sequence'"
    ).fetchone()
    sequence = int(sequence_row["value"]) + 1
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='activation_sequence'",
        (str(sequence),),
    )
    identity = {
        "work_id": work["work_id"],
        "actor_id": actor_id,
        "revision": work["revision"],
        "epoch": work["control_epoch"],
        "endpoint_generation": actor["generation"],
        "assignment_generation": assignment_generation,
        "reason": reason,
        "manifest": manifest,
        "sequence": sequence,
    }
    activation_id = f"cont-{_digest(identity)[:24]}"
    event_id = f"continuity-{_digest({'activation_id': activation_id})[:24]}"
    now = core.utc_now()
    connection.execute(
        """INSERT INTO activations(
               activation_id, work_id, actor_id, work_revision, control_epoch,
               endpoint_generation, assignment_generation, status, reason,
               manifest_json, wake_target_json, not_before, created_at, claimed_at
           ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,NULL)""",
        (
            activation_id,
            work["work_id"],
            actor_id,
            work["revision"],
            work["control_epoch"],
            actor["generation"],
            assignment_generation,
            reason,
            _json(manifest),
            _json(wake_target),
            not_before,
            now,
        ),
    )
    connection.execute(
        """INSERT INTO outbox(
               activation_id, event_id, status, attempts, last_error, updated_at
           ) VALUES(?,?,'pending',0,NULL,?)""",
        (activation_id, event_id, now),
    )
    return activation_id


def _schedule_obligation_activations(
    connection: sqlite3.Connection,
    *,
    work: sqlite3.Row,
    actor_id: str,
    obligation: sqlite3.Row,
) -> str | None:
    if _effective_explicit_stop(
        connection, work=work, actor_id=actor_id
    ) is not None:
        return None
    manifest = [f"obligation:{obligation['obligation_id']}"]
    assignment = _create_activation(
        connection,
        work=work,
        actor_id=actor_id,
        reason="obligation_assigned",
        manifest=manifest,
        assignment_generation=int(obligation["generation"]),
    )
    reminder_count = int(obligation["max_reminders"])
    interval = float(obligation["reminder_seconds"])
    scheduled_at = datetime.now(UTC)
    for index in range(reminder_count):
        _create_activation(
            connection,
            work=work,
            actor_id=actor_id,
            reason="obligation_reminder",
            manifest=manifest,
            assignment_generation=int(obligation["generation"]),
            not_before=(
                scheduled_at + timedelta(seconds=interval * (index + 1))
            ).isoformat(timespec="milliseconds"),
        )
    connection.execute(
        "UPDATE obligations SET reminder_count=? WHERE obligation_id=?",
        (reminder_count, obligation["obligation_id"]),
    )
    return assignment


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _result_reference(project_root: Path, value: str) -> dict[str, Any]:
    reference: dict[str, Any] = {
        "reference": value,
        "reference_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "content_status": "unverified",
    }
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = project_root.expanduser().resolve() / candidate
    try:
        candidate = candidate.resolve()
        candidate.relative_to(project_root.expanduser().resolve())
    except (OSError, ValueError):
        return reference
    if not candidate.is_file():
        return reference
    try:
        reference.update(
            content_status="verified",
            path=str(candidate),
            content_sha256=core.sha256_file(candidate),
            size_bytes=candidate.stat().st_size,
        )
    except OSError as error:
        reference.update(content_status="unavailable", error=str(error)[:240])
    return reference


def _work_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **{
            key: row[key]
            for key in row.keys()  # noqa: SIM118
            if key != "references_json"
        },
        "references": _decode(row["references_json"], []),
    }


def _obligation_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = {key: row[key] for key in row.keys()}  # noqa: SIM118
    value["required"] = bool(value["required"])
    return value


def _activation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **{
            key: row[key]
            for key in row.keys()  # noqa: SIM118
            if not key.endswith("_json")
        },
        "manifest": _decode(row["manifest_json"], []),
        "wake_target": _decode(row["wake_target_json"], {}),
    }


def _wait_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "work_id": row["work_id"],
        "generation": row["generation"],
        "mode": row["mode"],
        "sources": _decode(row["sources_json"], []),
        "handled_results": _decode(row["handled_json"], []),
        "updated_at": row["updated_at"],
    }


def _assignment_checkpoint_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "obligation_id": row["obligation_id"],
        "assignment_generation": row["assignment_generation"],
        "revision": row["revision"],
        "mode": row["mode"],
        "summary": row["summary"],
        "next_action": row["next_action"],
        "wait_mode": row["wait_mode"],
        "sources": _decode(row["sources_json"], []),
        "handled_results": _decode(row["handled_json"], []),
        "updated_at": row["updated_at"],
    }


def _result_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "outcome_id": row["outcome_id"],
        "source_key": row["source_key"],
        "status": row["status"],
        "event_id": row["event_id"],
        "digest": row["digest"],
        "observed_facts": _decode(row["data_json"], {}),
        "recorded_at": row["recorded_at"],
    }


def _note_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "work_id": row["work_id"],
        "revision": row["revision"],
        "actor_id": row["actor_id"],
        "text": row["text"],
        "references": _decode(row["references_json"], []),
        "updated_at": row["updated_at"],
    }


def _repository_snapshot(project_root: Path) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    try:
        head = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ["git", "-C", str(project), "status", "--porcelain=v1"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "unavailable", "reason": str(error)[:240]}
    if head.returncode != 0 or status.returncode != 0:
        return {
            "status": "unavailable",
            "reason": "project is not a readable Git worktree",
        }
    changed = [line for line in status.stdout.splitlines() if line]
    return {
        "status": "captured",
        "head": head.stdout.strip(),
        "dirty": bool(changed),
        "changed_path_count": len(changed),
    }
