"""Opt-in local observations routed through the existing continuity outbox."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import continuity, core, platform_runtime

FIELDS = frozenset(
    {
        "observation",
        "hypothesis",
        "expected",
        "actual",
        "impact",
        "version",
        "platform",
        "identifiers",
        "evidence",
        "reproduction",
        "workaround",
    }
)
REQUIRED_FIELDS = frozenset({"observation", "expected", "actual", "impact"})
MAX_OBSERVATIONS = 64
MAX_DETAILS_BYTES = 8000


def _root(root: Path, state_dir: str) -> Path:
    return continuity.continuity_root(root, state_dir=state_dir) / "feedback"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _read(path: Path, kind: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("artifact is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
        ):
            raise ValueError("unsupported feedback schema")
        if value.get("kind") != kind:
            raise ValueError("unexpected feedback kind")
        if kind == "ORCHESTRATOR_FEEDBACK_CONFIG":
            core.validate_event_id(value.get("recipient_actor"))
            allowed = value.get("allowed_fields")
            if (
                not isinstance(allowed, list)
                or not allowed
                or any(
                    not isinstance(field, str) or field not in FIELDS
                    for field in allowed
                )
            ):
                raise ValueError("invalid export allowlist")
        else:
            core.validate_event_id(value.get("report_id"))
            if value.get("classification") not in {"defect", "suggestion"}:
                raise ValueError("invalid classification")
            observations = value.get("observations")
            if (
                not isinstance(observations, list)
                or not 1 <= len(observations) <= MAX_OBSERVATIONS
            ):
                raise ValueError("invalid observations")
            for item in observations:
                if not isinstance(item, dict) or not isinstance(
                    item.get("details"), dict
                ):
                    raise ValueError("invalid observation shape")
                details = item["details"]
                if (
                    set(details) - FIELDS
                    or REQUIRED_FIELDS - set(details)
                    or any(
                        not isinstance(detail, str) or not detail.strip()
                        for detail in details.values()
                    )
                    or item.get("digest") != _digest(details)
                ):
                    raise ValueError("invalid observation details or digest")
            if "delivery" not in value or not isinstance(value.get("truncated"), bool):
                raise ValueError("invalid feedback metadata")
            delivery = value["delivery"]
            if delivery is not None:
                if not isinstance(delivery, dict):
                    raise ValueError("invalid delivery")
                for key in ("work_id", "sender_actor", "recipient_actor", "request_id"):
                    core.validate_event_id(delivery.get(key))
                if (
                    not isinstance(delivery.get("message"), str)
                    or not delivery["message"]
                ):
                    raise ValueError("invalid frozen message")
        return value
    except (OSError, ValueError, core.OrchestratorError) as error:
        raise continuity.ContinuityError(
            f"cannot read feedback artifact: {error}"
        ) from error


def configure(
    root: Path,
    *,
    recipient_actor: str,
    allowed_fields: list[str],
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """The operator authorizes a destination and an explicit export allowlist."""
    core.validate_event_id(recipient_actor)
    if not allowed_fields or set(allowed_fields) - FIELDS:
        raise continuity.ContinuityError(
            "an explicit valid feedback field allowlist is required"
        )
    actors = continuity.actor_status(
        root, actor_id=recipient_actor, state_dir=state_dir
    )["actors"]
    if not actors or not actors[0]["active"]:
        raise continuity.ContinuityError(
            "feedback destination must be an active explicitly registered actor"
        )
    value = {
        "kind": "ORCHESTRATOR_FEEDBACK_CONFIG",
        "schema_version": 1,
        "recipient_actor": recipient_actor,
        "allowed_fields": sorted(set(allowed_fields)),
    }
    directory = _root(root, state_dir)
    with platform_runtime.exclusive_file_lock(directory / "feedback.lock"):
        core.atomic_json(directory / "config.json", value)
    return value


def record(
    root: Path,
    *,
    cause: str,
    classification: str,
    details: dict[str, Any],
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Record bounded caller-selected facts; never collect logs or send implicitly."""
    if not isinstance(cause, str) or not cause.strip() or len(cause) > 256:
        raise continuity.ContinuityError(
            "cause must be a stable nonempty signature of at most 256 characters"
        )
    if classification not in {"defect", "suggestion"}:
        raise continuity.ContinuityError("classification must be defect or suggestion")
    if (
        not isinstance(details, dict)
        or set(details) - FIELDS
        or REQUIRED_FIELDS - set(details)
    ):
        raise continuity.ContinuityError(
            "feedback requires observation, expected, actual and impact; "
            "unknown fields are rejected"
        )
    if any(
        not isinstance(value, str) or not value.strip() for value in details.values()
    ):
        raise continuity.ContinuityError("feedback details must be nonempty strings")
    if len(core.json_text(details).encode("utf-8")) > MAX_DETAILS_BYTES:
        raise continuity.ContinuityError("feedback details exceed 8000 bytes")
    report_id = "feedback-" + _digest([classification, cause.strip()])[:32]
    directory = _root(root, state_dir)
    path = directory / "reports" / f"{report_id}.json"
    with platform_runtime.exclusive_file_lock(directory / "feedback.lock"):
        report = (
            _read(path, "ORCHESTRATOR_OPERATIONAL_FEEDBACK")
            if path.exists()
            else {
                "kind": "ORCHESTRATOR_OPERATIONAL_FEEDBACK",
                "schema_version": 1,
                "report_id": report_id,
                "cause": cause.strip(),
                "classification": classification,
                "observations": [],
                "truncated": False,
                "delivery": None,
            }
        )
        digest = _digest(details)
        duplicate = any(item["digest"] == digest for item in report["observations"])
        if not duplicate:
            if len(report["observations"]) == MAX_OBSERVATIONS:
                if not report["truncated"]:
                    report["truncated"] = True
                    core.atomic_json(path, report)
            else:
                report["observations"].append(
                    {
                        "digest": digest,
                        "recorded_at": core.utc_now(),
                        "details": details,
                    }
                )
                core.atomic_json(path, report)
    return {
        "report_id": report_id,
        "path": str(path),
        "duplicate": duplicate,
        "observation_count": len(report["observations"]),
        "truncated": report["truncated"],
    }


def show(
    root: Path, *, report_id: str, state_dir: str = core.DEFAULT_STATE_DIR
) -> dict[str, Any]:
    core.validate_event_id(report_id)
    return _read(
        _root(root, state_dir) / "reports" / f"{report_id}.json",
        "ORCHESTRATOR_OPERATIONAL_FEEDBACK",
    )


def send(
    root: Path,
    *,
    report_id: str,
    work_id: str,
    sender_actor: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Freeze one privacy-filtered FYI request; replay only its original identity."""
    core.validate_event_id(report_id)
    core.validate_event_id(work_id)
    core.validate_event_id(sender_actor)
    directory = _root(root, state_dir)
    path = directory / "reports" / f"{report_id}.json"
    with platform_runtime.exclusive_file_lock(directory / "feedback.lock"):
        report = _read(path, "ORCHESTRATOR_OPERATIONAL_FEEDBACK")
        if report["delivery"] is None:
            config = _read(directory / "config.json", "ORCHESTRATOR_FEEDBACK_CONFIG")
            allowed = config["allowed_fields"]
            if not isinstance(allowed, list) or not allowed or set(allowed) - FIELDS:
                raise continuity.ContinuityError("invalid feedback export allowlist")
            if not report["observations"]:
                raise continuity.ContinuityError("feedback has no observations")
            # Export only the frozen allowlisted snapshot, never local report paths.
            message = core.json_text(
                {
                    "kind": "ORCHESTRATOR_OPERATIONAL_FEEDBACK_FYI",
                    "schema_version": 1,
                    "report_id": report_id,
                    "classification": report["classification"],
                    "details": {
                        key: value
                        for key, value in report["observations"][0]["details"].items()
                        if key in allowed
                    },
                    "instruction": (
                        "FYI only. Triage is not permission to implement, publish "
                        "or reply. Inspect current authority before any action."
                    ),
                }
            )
            report["delivery"] = {
                "work_id": work_id,
                "sender_actor": sender_actor,
                "recipient_actor": config["recipient_actor"],
                "message": message,
                "request_id": report_id,
                "status": "pending",
                "error": None,
            }
            core.atomic_json(path, report)
        delivery = report["delivery"]
        if delivery["work_id"] != work_id or delivery["sender_actor"] != sender_actor:
            raise continuity.ContinuityError(
                "feedback route identity conflicts; "
                "the original owner/work must recover it"
            )
        try:
            request = continuity.request_send(
                root,
                work_id=work_id,
                request_id=delivery["request_id"],
                idempotency_key=delivery["request_id"],
                sender_actor=sender_actor,
                recipient_actor=delivery["recipient_actor"],
                return_actor=sender_actor,
                message=delivery["message"],
                requires_reply=False,
                required=False,
                max_reminders=0,
                state_dir=state_dir,
            )
        except (
            continuity.ContinuityError,
            core.OrchestratorError,
            OSError,
            sqlite3.Error,
        ):
            delivery["status"] = "fallback_required"
            delivery["error"] = (
                "managed route failed; inspect continuity diagnostics "
                "and recover the same request identity"
            )
            core.atomic_json(path, report)
            return {
                "report_id": report_id,
                "path": str(path),
                "status": "fallback_required",
                "request_id": delivery["request_id"],
                "fallback": (
                    "Explicit operator/host-tool handoff only; "
                    "do not duplicate a retained outbox request."
                ),
            }
        delivery["status"] = "submitted"
        delivery["error"] = None
        core.atomic_json(path, report)
    return {
        "report_id": report_id,
        "path": str(path),
        "status": "submitted",
        "request_id": delivery["request_id"],
        "request_status": request["status"],
        "idempotent": request["idempotent"],
    }
