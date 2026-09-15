"""Read-only, bounded metadata for one retained native local check."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, core, schemas

MAX_READ_BYTES = 8 * 1024 * 1024
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_DIAGNOSTICS = 16
ROLES = ("result", "evidence", "event")
KINDS = {"worker", "check", "ci", "pr"}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
TIME_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)\Z", re.ASCII
)
STATUSES = {"starting", "running", "passed", "failed", "errored", "cancelled"}
TERMINAL = STATUSES - {"starting", "running"}


def target_argument(value: str) -> str:
    kind, separator, identity = value.partition(":")
    if not separator or kind not in KINDS or not ID_PATTERN.fullmatch(identity):
        raise argparse.ArgumentTypeError(
            "target must be worker/check/ci/pr:ID with an ASCII ID of 1..128 characters"
        )
    return value


def _time(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 40
        or not TIME_PATTERN.fullmatch(value)
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0
        )
    except ValueError:
        return False


def _project_id(value: Any) -> bool:
    try:
        return (
            isinstance(value, str)
            and 1 <= len(value.encode("utf-8")) <= 256
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
        )
    except UnicodeError:
        return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def _finite_number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _producer_shape(value: dict[str, Any], schema_name: str) -> bool:
    """Check the pinned top-level projection, not nested execution semantics."""
    schema = schemas.load(schema_name)
    if any(key not in value for key in schema.get("required", [])):
        return False
    type_checks = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: type(item) is int,
        "number": _finite_number,
        "boolean": lambda item: type(item) is bool,
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "null": lambda item: item is None,
    }
    for key, rule in schema.get("properties", {}).items():
        if key not in value:
            continue
        item = value[key]
        expected_types = rule.get("type")
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if expected_types and not any(type_checks[t](item) for t in expected_types):
            return False
        if "const" in rule and item != rule["const"]:
            return False
        if "enum" in rule and item not in rule["enum"]:
            return False
        if "$ref" in rule and not isinstance(item, dict):
            return False
        if isinstance(item, str):
            if len(item) < rule.get("minLength", 0) or len(item) > rule.get(
                "maxLength", math.inf
            ):
                return False
            if rule.get("format") == "date-time" and not _time(item):
                return False
            # Legacy evidence schemas used POSIX-only path patterns. The pinned
            # native projection accepts absolute paths on the current platform.
            if (key.endswith("_path") or key == "check_dir") and "pattern" in rule:
                if not Path(item).is_absolute() or "\x00" in item:
                    return False
            elif "pattern" in rule and re.search(rule["pattern"], item) is None:
                return False
            if (
                key.endswith("_sha256") or key == "fingerprint"
            ) and not DIGEST_PATTERN.fullmatch(item):
                return False
        if type(item) in (int, float):
            if not _finite_number(item):
                return False
            if item < rule.get("minimum", -math.inf) or item > rule.get(
                "maximum", math.inf
            ):
                return False
            if "exclusiveMinimum" in rule and item <= rule["exclusiveMinimum"]:
                return False
    return True


class _Reader:
    def __init__(self, project: Path, state: Path, budget: int):
        self.project = project
        self.state = state
        self.remaining = budget
        self.errors: set[tuple[str, str | None]] = set()
        self.omissions: set[tuple[str, str | None]] = set()

    def error(
        self, code: str, role: str | None = None, *, omission: bool = False
    ) -> None:
        (self.omissions if omission else self.errors).add((code, role))
        if omission and code in {"unsupported_schema", "unsupported_producer"}:
            self.errors.add((code, role))

    def path(self, value: Any, role: str | None) -> Path | None:
        if not isinstance(value, str) or not value or "\x00" in value:
            self.error("unsupported_value", role)
            return None
        try:
            path = Path(value)
            resolved = (path if path.is_absolute() else self.project / path).resolve()
            if not resolved.is_relative_to(self.state):
                self.error("path_outside_state", role)
                return None
            return resolved
        except (OSError, RuntimeError, ValueError):
            self.error("artifact_unreadable", role, omission=True)
            return None

    def read(
        self, path: Path | None, role: str | None, *, optional: bool = False
    ) -> tuple[bytes | None, str]:
        if path is None:
            return None, "unreadable"
        if self.remaining <= 0:
            self.error("read_budget_exceeded", role, omission=True)
            return None, "budget_limited"
        try:
            if not path.resolve().is_relative_to(self.state):
                self.error("path_outside_state", role)
                return None, "unreadable"
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                self.error("artifact_unreadable", role, omission=True)
                return None, "unreadable"
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or not path.resolve().is_relative_to(self.state)
                ):
                    self.error("snapshot_changed", role)
                    return None, "unreadable"
                raw = stream.read(self.remaining + 1)
            self.remaining -= len(raw)
            if self.remaining < 0:
                self.error("read_budget_exceeded", role, omission=True)
                return None, "budget_limited"
            return raw, "present"
        except FileNotFoundError:
            if not optional:
                self.error("artifact_missing", role, omission=True)
            return None, "missing"
        except (OSError, RuntimeError, ValueError):
            self.error("artifact_unreadable", role, omission=True)
            return None, "unreadable"

    def object(
        self, raw: bytes | None, role: str | None, kind: str
    ) -> dict[str, Any] | None:
        if raw is None:
            return None
        try:
            value = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
        except (UnicodeError, ValueError, RecursionError):
            self.error("invalid_json", role)
            self.error("unsupported_schema", role, omission=True)
            return None
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
        ):
            self.error("unsupported_schema", role, omission=True)
            return None
        if value.get("kind") != kind:
            self.error("unsupported_producer", role, omission=True)
            return None
        return value

    def link(self, value: Any, expected: Path | None, role: str | None) -> bool:
        actual = self.path(value, role)
        if expected is None or actual != expected:
            self.error("identity_mismatch", role)
            return False
        return True


def _diagnostics(
    values: set[tuple[str, str | None]],
) -> tuple[list[dict[str, str]], bool]:
    ordered = sorted(values, key=lambda item: (item[0], item[1] or ""))
    truncated = len(ordered) > MAX_DIAGNOSTICS
    if truncated:
        ordered = [*ordered[: MAX_DIAGNOSTICS - 1], ("diagnostics_truncated", None)]
        ordered.sort(key=lambda item: (item[0], item[1] or ""))
    return [
        {"code": code, **({"artifact_role": role} if role else {})}
        for code, role in ordered
    ], truncated


def operation_evidence(
    project_root: Path, *, target: str, state_dir: str = core.DEFAULT_STATE_DIR
) -> dict[str, Any]:
    """Return observations, never execution authority or acceptance."""
    target_argument(target)
    target_kind, operation_id = target.split(":", 1)
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_OPERATION_EVIDENCE",
        "engine_version": __version__,
        "target": {"kind": target_kind, "operation_id": operation_id},
        "source": {"project_id": None, "identity_state": "unknown"},
        "attempt": {"identity": None, "retry_of": None, "state": "unknown"},
        "candidate": {"identity": None, "provenance": None, "state": "unknown"},
        "operation": {"status": "unknown", "terminal": None, "finished_at": None},
        "snapshot": {"observed_at": core.utc_now(), "consistency": "unknown"},
        "completeness": "unsupported",
        "artifacts": [],
        "errors": [],
        "omissions": [],
        "diagnostics_truncated": False,
    }
    if target_kind != "check":
        report["omissions"] = [{"code": "unsupported_producer"}]
        return report
    project = project_root.resolve()
    state = core.state_root(project, state_dir=state_dir).resolve()
    reader = _Reader(project, state, MAX_READ_BYTES)
    directory = state / "checks" / operation_id
    descriptor_path = reader.path(str(directory / "check.json"), None)
    descriptor_raw, descriptor_presence = reader.read(descriptor_path, None)
    descriptor = reader.object(descriptor_raw, None, "ORCHESTRATOR_LOCAL_CHECK")
    owner_path = reader.path(str(directory / "operation-owner.json"), None)
    owner_raw, owner_presence = reader.read(owner_path, None, optional=True)
    owner = reader.object(owner_raw, None, "ORCHESTRATOR_CHECK_RESULT_OWNER")
    if owner is not None:
        if not _producer_shape(owner, "check-operation-owner"):
            reader.error("unsupported_schema", omission=True)
        elif (
            owner.get("operation_id") != operation_id
            or owner.get("operation_type") != "local_check"
        ):
            if descriptor_raw is not None or owner.get("operation_id") != operation_id:
                report["errors"] = [{"code": "identity_mismatch"}]
            report["omissions"] = [{"code": "unsupported_producer"}]
            return report
    if (
        descriptor_raw is not None
        and descriptor is None
        and ("unsupported_producer", None) in reader.omissions
    ):
        report["omissions"] = [{"code": "unsupported_producer"}]
        return report
    if descriptor_presence == "missing":
        reader.error("target_missing", omission=True)
    paths: dict[str, Path | None] = {
        "result": reader.path(str(directory / "verification-result.json"), "result"),
        "evidence": reader.path(str(directory / "evidence.json"), "evidence"),
        "event": None,
    }
    final = False
    if descriptor is not None:
        if not _producer_shape(descriptor, "local-check"):
            reader.error("unsupported_schema", omission=True)
            descriptor = None
        elif descriptor.get("check_id") != operation_id:
            reader.error("identity_mismatch")
            descriptor = None
        else:
            reader.link(descriptor["check_dir"], directory.resolve(), None)
            status = descriptor.get("status")
            if isinstance(status, str) and status in STATUSES:
                report["operation"]["status"] = status
                report["operation"]["terminal"] = status in TERMINAL
                final = status in TERMINAL
            else:
                reader.error("unsupported_value")
            if _time(descriptor.get("finished_at")):
                report["operation"]["finished_at"] = descriptor["finished_at"]
            elif final:
                reader.error("unsupported_value")
            for role in ("result", "evidence"):
                if descriptor.get(role + "_path") is not None:
                    reader.link(descriptor[role + "_path"], paths[role], role)
                elif final:
                    reader.error("metadata_missing", role, omission=True)
            if descriptor.get("event_path") is not None:
                event_path = reader.path(descriptor["event_path"], "event")
                if (
                    event_path is not None
                    and event_path.parent != (state / "events").resolve()
                ):
                    reader.error("identity_mismatch", "event")
                else:
                    paths["event"] = event_path
            if paths["event"] is None:
                reader.error("metadata_missing", "event", omission=True)
    raw: dict[str, bytes | None] = {}
    objects: dict[str, dict[str, Any] | None] = {}
    headers = {
        "result": "ORCHESTRATOR_VERIFICATION_RESULT",
        "evidence": "ORCHESTRATOR_LOCAL_CHECK_EVIDENCE",
        "event": "ORCHESTRATOR_TERMINAL",
    }
    shape_names = {
        "result": "verification-result",
        "evidence": "local-check-evidence",
        "event": "followup-terminal-event",
    }
    for role in ROLES:
        if (
            role == "event"
            and paths[role] is None
            and not any(
                code == "path_outside_state" and attributed_role == role
                for code, attributed_role in reader.errors
            )
        ):
            raw[role], presence = None, "missing"
            reader.error("artifact_missing", role, omission=True)
        else:
            raw[role], presence = reader.read(paths[role], role)
        objects[role] = reader.object(raw[role], role, headers[role])
        if objects[role] is not None and not _producer_shape(
            objects[role], shape_names[role]
        ):
            reader.error("unsupported_schema", role, omission=True)
            objects[role] = None
        report["artifacts"].append(
            {
                "role": role,
                "presence": presence,
                "observed": None
                if raw[role] is None
                else {
                    "sha256": hashlib.sha256(raw[role]).hexdigest(),
                    "size_bytes": len(raw[role]),
                },
                "retained": [],
                "integrity": "unavailable" if raw[role] is None else "observed_only",
            }
        )
    valid: dict[str, bool] = {}
    for role in ROLES:
        obj = objects[role]
        valid[role] = obj is not None and descriptor is not None
        if obj is None:
            continue
        identity = obj.get("operation_id" if role == "event" else "check_id")
        if identity != operation_id:
            reader.error("identity_mismatch", role)
            valid[role] = False
        if role == "event":
            event_id = obj.get("event_id")
            if (
                not isinstance(event_id, str)
                or not ID_PATTERN.fullmatch(event_id)
                or paths[role] is None
                or paths[role].name != event_id + ".json"
                or not _time(obj.get("created_at"))
            ):
                reader.error("unsupported_schema", role, omission=True)
                valid[role] = False
            if obj.get("source_kind") != "local_check" or obj.get("event_id") != (
                descriptor or {}
            ).get("event_id"):
                reader.error("identity_mismatch", role)
                valid[role] = False
            if obj.get("terminal_status") != (
                "completed" if report["operation"]["status"] == "passed" else "failed"
            ):
                reader.error("identity_mismatch", role)
                valid[role] = False
            pid = obj.get("project_id")
            if pid is None:
                reader.error("metadata_missing", "event", omission=True)
                valid[role] = False
            elif not _project_id(pid):
                reader.error("unsupported_value", "event")
                valid[role] = False
            elif valid[role]:
                report["source"] = {"project_id": pid, "identity_state": "retained"}
        elif descriptor is not None:
            if role == "evidence" and (
                obj.get("execution") != descriptor["execution"]
                or obj.get("wake_policy") not in ("always", "on-failure", "never")
                or not isinstance(obj.get("plan"), dict)
                or not _time(obj.get("recorded_at"))
            ):
                reader.error("unsupported_schema", role, omission=True)
                valid[role] = False
            if role == "result" and (
                not _time(obj.get("finished_at"))
                or ("commands" in obj and not isinstance(obj["commands"], list))
            ):
                reader.error("unsupported_schema", role, omission=True)
                valid[role] = False
            for field in ("suite", "fingerprint"):
                if not isinstance(obj.get(field), str) or obj.get(
                    field
                ) != descriptor.get(field):
                    reader.error("identity_mismatch", role)
                    valid[role] = False
            if role == "result" and (
                obj.get("status") != descriptor.get("status")
                or obj.get("finished_at") != descriptor.get("finished_at")
            ):
                reader.error("identity_mismatch", role)
                valid[role] = False
    for binder, basis, bound_roles in (
        ("event", "terminal_event", ("result", "evidence")),
        ("evidence", "check_evidence", ("result",)),
    ):
        obj = objects[binder]
        if not valid[binder] or obj is None:
            continue
        for role in bound_roles:
            linked = reader.link(obj.get(role + "_path"), paths[role], binder)
            digest = obj.get(role + "_sha256")
            if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
                reader.error("unsupported_value", binder)
                reader.error("metadata_missing", role, omission=True)
                continue
            if linked:
                report["artifacts"][ROLES.index(role)]["retained"].append(
                    {"sha256": digest, "basis": basis}
                )
    for item in report["artifacts"]:
        item["retained"].sort(key=lambda binding: binding["basis"])
        observed, retained = item["observed"], item["retained"]
        if len({b["sha256"] for b in retained}) > 1:
            reader.error("conflicting_retained_bindings", item["role"])
        if observed is not None and retained:
            item["integrity"] = (
                "matched"
                if all(b["sha256"] == observed["sha256"] for b in retained)
                else "mismatch"
            )
            if item["integrity"] == "mismatch":
                reader.error("digest_mismatch", item["role"])
        if item["role"] != "event" and len(retained) != (
            2 if item["role"] == "result" else 1
        ):
            reader.error("metadata_missing", item["role"], omission=True)
    consistency = "unknown"
    if descriptor is not None and not final:
        consistency = "unsealed"
        reader.error("publication_incomplete", omission=True)
    elif final and descriptor_raw is not None and raw["event"] is not None:
        again, _ = reader.read(descriptor_path, None)
        event_again, _ = reader.read(paths["event"], "event")
        owner_again, owner_again_presence = reader.read(owner_path, None, optional=True)
        if (
            again != descriptor_raw
            or event_again != raw["event"]
            or owner_again != owner_raw
            or owner_again_presence != owner_presence
        ):
            consistency = "changed"
            reader.error("snapshot_changed")
        elif owner_presence not in {"missing", "present"}:
            consistency = "unknown"
        else:
            consistency = "stable"
    report["snapshot"]["consistency"] = consistency
    errors, et = _diagnostics(reader.errors)
    omissions, ot = _diagnostics(reader.omissions)
    report.update(errors=errors, omissions=omissions, diagnostics_truncated=et or ot)
    report["completeness"] = (
        "complete"
        if not omissions and consistency == "stable"
        else "partial"
        if descriptor_raw is not None or any(raw.values())
        else "unavailable"
    )
    assert (
        len(json.dumps(report, ensure_ascii=True).encode("utf-8")) <= MAX_ENVELOPE_BYTES
    )
    return report
