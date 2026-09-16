"""Read-only applicability envelope for retained native local checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__, core, operation_applicability
from . import operation_evidence as v1

MAX_READ_BYTES = 1024 * 1024
MAX_ENVELOPE_BYTES = 160 * 1024
MAX_DIAGNOSTICS = 16


def _diagnostics(values: set[str]) -> tuple[list[dict[str, str]], bool]:
    ordered = sorted(values)
    truncated = len(ordered) > MAX_DIAGNOSTICS
    if truncated:
        ordered = [*ordered[: MAX_DIAGNOSTICS - 1], "diagnostics_truncated"]
        ordered.sort()
    return [{"code": code} for code in ordered], truncated


def _empty_applicability(state: str) -> dict[str, Any]:
    return {
        "state": state,
        "attempt_id": None,
        "captured_at": None,
        "source": {
            "project_id": None,
            "source_namespace": None,
            "native_location_binding": None,
            "binding_state": "unknown",
        },
        "declaration": None,
        "declaration_sha256": None,
        "input_raw_sha256": None,
        "artifact_sha256": None,
        "request_fingerprint": None,
        "suite_fingerprint": None,
        "retry_of": None,
        "terminal_binding": {"result": "unknown", "evidence": "unknown"},
        "assurance": {
            "declaration_binding": "unknown",
            "executed_candidate": "unknown",
            "criteria_fulfillment": "unknown",
        },
    }


def _bounded(report: dict[str, Any]) -> dict[str, Any]:
    assert (
        len(json.dumps(report, ensure_ascii=True).encode("utf-8"))
        <= MAX_ENVELOPE_BYTES
    )
    return report


def operation_evidence_v2(
    project_root: Path,
    *,
    target: str,
    state_dir: str = core.DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Return bounded declaration-binding evidence without acceptance claims."""

    v1.target_argument(target)
    target_kind, operation_id = target.split(":", 1)
    project = project_root.expanduser().resolve()
    state = core.state_root(project, state_dir=state_dir).resolve()
    reader = v1._Reader(project, state, MAX_READ_BYTES)
    base_capture: dict[str, Any] = {}
    base = v1.operation_evidence(
        project,
        target=target,
        state_dir=state_dir,
        contract_version=1,
        _reader=reader,
        _capture=base_capture,
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_OPERATION_EVIDENCE_V2",
        "contract_version": 2,
        "engine_version": __version__,
        "target": base["target"],
        "operation": base["operation"],
        "terminal_evidence": base,
        "applicability": _empty_applicability("unsupported"),
        "snapshot": {
            "observed_at": core.utc_now(),
            "consistency": base["snapshot"]["consistency"],
        },
        "completeness": "unsupported",
        "errors": [],
        "omissions": [],
        "diagnostics_truncated": False,
    }
    if target_kind != "check":
        report["omissions"] = [{"code": "unsupported_producer"}]
        return _bounded(report)

    directory = state / "checks" / operation_id
    errors: set[str] = set()
    omissions: set[str] = set()
    observed: list[tuple[str, Path, bytes | None, str, bool]] = []

    def retain(
        role: str,
        path: Path | None,
        raw: bytes | None,
        presence: str,
        *,
        optional: bool,
    ) -> None:
        if path is not None:
            observed.append((role, path, raw, presence, optional))

    retain(
        "descriptor",
        base_capture.get("descriptor_path"),
        base_capture.get("descriptor_raw"),
        base_capture.get("descriptor_presence", "unreadable"),
        optional=False,
    )
    retain(
        "owner",
        base_capture.get("owner_path"),
        base_capture.get("owner_raw"),
        base_capture.get("owner_presence", "unreadable"),
        optional=True,
    )
    for role in v1.ROLES:
        retain(
            role,
            base_capture.get("paths", {}).get(role),
            base_capture.get("raw", {}).get(role),
            base_capture.get("presences", {}).get(role, "unreadable"),
            optional=False,
        )

    input_path = directory / "applicability-input.json"
    artifact_path = directory / "applicability.json"
    raw_input, input_presence = reader.read(input_path, "input", optional=True)
    raw_artifact, artifact_presence = reader.read(
        artifact_path, "artifact", optional=True
    )
    retain("input", input_path, raw_input, input_presence, optional=True)
    retain("artifact", artifact_path, raw_artifact, artifact_presence, optional=True)

    descriptor = base_capture.get("descriptor")
    if descriptor is None:
        report["applicability"] = _empty_applicability("unknown")
        report["completeness"] = "unavailable"
        omissions.add(
            "target_missing"
            if base_capture.get("descriptor_presence") == "missing"
            else "descriptor_unreadable"
        )
    elif not v1._producer_shape(descriptor, "local-check") or descriptor.get(
        "check_id"
    ) != operation_id:
        report["applicability"] = _empty_applicability("conflicted")
        report["completeness"] = "conflicted"
        errors.add("descriptor_invalid")
    elif descriptor.get("applicability") is None:
        report["applicability"] = _empty_applicability("unsupported")
        report["completeness"] = "unsupported"
        omissions.add("legacy_without_applicability")
    else:
        metadata = descriptor.get("applicability")
        if raw_input is None or raw_artifact is None:
            report["applicability"] = _empty_applicability("conflicted")
            report["completeness"] = "conflicted"
            errors.add("applicability_missing")
        else:
            try:
                artifact = operation_applicability.validate_artifact(
                    project,
                    state_dir=state_dir,
                    operation_id=operation_id,
                    directory=directory,
                    descriptor=descriptor,
                    require_current_source=False,
                    validate_retry=False,
                    raw_input=raw_input,
                    raw_artifact=raw_artifact,
                )
                retry = artifact.get("retry_of")
                if retry is not None:
                    predecessor_capture: dict[str, Any] = {}
                    predecessor_report = v1.operation_evidence(
                        project,
                        target=f"check:{retry['operation_id']}",
                        state_dir=state_dir,
                        contract_version=1,
                        _reader=reader,
                        _capture=predecessor_capture,
                    )
                    predecessor_directory = (
                        state / "checks" / retry["operation_id"]
                    )
                    predecessor_input = (
                        predecessor_directory / "applicability-input.json"
                    )
                    predecessor_artifact = (
                        predecessor_directory / "applicability.json"
                    )
                    predecessor_input_raw, predecessor_input_presence = reader.read(
                        predecessor_input, "predecessor_input", optional=False
                    )
                    predecessor_artifact_raw, predecessor_artifact_presence = (
                        reader.read(
                            predecessor_artifact,
                            "predecessor_artifact",
                            optional=False,
                        )
                    )
                    for role, path, raw, presence, optional in (
                        (
                            "predecessor_descriptor",
                            predecessor_capture.get("descriptor_path"),
                            predecessor_capture.get("descriptor_raw"),
                            predecessor_capture.get(
                                "descriptor_presence", "unreadable"
                            ),
                            False,
                        ),
                        (
                            "predecessor_owner",
                            predecessor_capture.get("owner_path"),
                            predecessor_capture.get("owner_raw"),
                            predecessor_capture.get("owner_presence", "unreadable"),
                            True,
                        ),
                        *(
                            (
                                f"predecessor_{role}",
                                predecessor_capture.get("paths", {}).get(role),
                                predecessor_capture.get("raw", {}).get(role),
                                predecessor_capture.get("presences", {}).get(
                                    role, "unreadable"
                                ),
                                False,
                            )
                            for role in v1.ROLES
                        ),
                        (
                            "predecessor_input",
                            predecessor_input,
                            predecessor_input_raw,
                            predecessor_input_presence,
                            False,
                        ),
                        (
                            "predecessor_artifact",
                            predecessor_artifact,
                            predecessor_artifact_raw,
                            predecessor_artifact_presence,
                            False,
                        ),
                    ):
                        retain(role, path, raw, presence, optional=optional)
                    predecessor_descriptor = predecessor_capture.get("descriptor")
                    if (
                        predecessor_descriptor is None
                        or predecessor_input_raw is None
                        or predecessor_artifact_raw is None
                    ):
                        raise operation_applicability.ApplicabilityError(
                            "retry predecessor observation is unavailable"
                        )
                    predecessor = operation_applicability.validate_artifact(
                        project,
                        state_dir=state_dir,
                        operation_id=retry["operation_id"],
                        directory=predecessor_directory,
                        descriptor=predecessor_descriptor,
                        require_current_source=False,
                        validate_retry=False,
                        raw_input=predecessor_input_raw,
                        raw_artifact=predecessor_artifact_raw,
                    )
                    operation_applicability.validate_predecessor_observation(
                        declaration=artifact["declaration"],
                        source=artifact["source"],
                        retry=retry,
                        report=predecessor_report,
                        descriptor=predecessor_descriptor,
                        artifact=predecessor,
                        artifact_sha256=hashlib.sha256(
                            predecessor_artifact_raw
                        ).hexdigest(),
                        objects=predecessor_capture.get("objects", {}),
                    )
            except (
                KeyError,
                OSError,
                core.OrchestratorError,
                operation_applicability.ApplicabilityError,
            ):
                report["applicability"] = _empty_applicability("conflicted")
                report["completeness"] = "conflicted"
                errors.add("applicability_invalid")
            else:
                current_source = operation_applicability.source_binding(
                    project, state_dir=state_dir
                )
                terminal = {}
                for role in ("result", "evidence"):
                    item = base_capture.get("objects", {}).get(role)
                    if base_capture.get("raw", {}).get(role) is None:
                        terminal[role] = (
                            "missing"
                            if base["operation"]["terminal"] is True
                            else "not_terminal"
                        )
                    else:
                        terminal[role] = (
                            "matched"
                            if item is not None
                            and operation_applicability.metadata_matches(
                                item.get("applicability"), metadata
                            )
                            else "mismatch"
                        )
                report["applicability"] = {
                    "state": (
                        "retained"
                        if artifact["source"] == current_source
                        else "conflicted"
                    ),
                    "attempt_id": artifact["attempt_id"],
                    "captured_at": artifact["captured_at"],
                    "source": {
                        "project_id": artifact["source"]["project_id"],
                        "source_namespace": artifact["source"]["source_namespace"],
                        "native_location_binding": artifact["source"][
                            "native_location_binding"
                        ],
                        "binding_state": (
                            "retained"
                            if artifact["source"] == current_source
                            else "mismatch"
                        ),
                    },
                    "declaration": artifact["declaration"],
                    "declaration_sha256": artifact["declaration_sha256"],
                    "input_raw_sha256": artifact["input_raw_sha256"],
                    "artifact_sha256": metadata["artifact_sha256"],
                    "request_fingerprint": artifact["request_fingerprint"],
                    "suite_fingerprint": artifact["suite_fingerprint"],
                    "retry_of": artifact["retry_of"],
                    "terminal_binding": terminal,
                    "assurance": artifact["assurance"],
                }
                if artifact["source"] != current_source:
                    report["completeness"] = "conflicted"
                    errors.add("source_mismatch")
                elif "mismatch" in terminal.values():
                    report["applicability"]["state"] = "conflicted"
                    report["completeness"] = "conflicted"
                    errors.add("terminal_binding_mismatch")
                elif (
                    base["operation"]["terminal"] is True
                    and "missing" in terminal.values()
                ):
                    report["applicability"]["state"] = "unknown"
                    report["completeness"] = "partial"
                    omissions.add("terminal_binding_missing")
                elif (
                    base["completeness"] == "complete"
                    and base["snapshot"]["consistency"] == "stable"
                    and set(terminal.values()) == {"matched"}
                ):
                    report["completeness"] = "complete"
                else:
                    report["completeness"] = "partial"

    changed = False
    for role, path, first_raw, first_presence, optional in observed:
        second_raw, second_presence = reader.read(path, role, optional=optional)
        if first_raw != second_raw or first_presence != second_presence:
            changed = True
    if changed:
        report["snapshot"]["consistency"] = "changed"
        report["completeness"] = "conflicted"
        if report["applicability"]["state"] == "retained":
            report["applicability"]["state"] = "conflicted"
        errors.add("snapshot_changed")
    elif base["operation"]["terminal"] is not True and descriptor is not None:
        report["snapshot"]["consistency"] = "unsealed"
    elif report["snapshot"]["consistency"] == "unknown" and descriptor is not None:
        report["snapshot"]["consistency"] = "stable"

    errors.update(code for code, _ in reader.errors)
    omissions.update(code for code, _ in reader.omissions)
    error_values, errors_truncated = _diagnostics(errors)
    omission_values, omissions_truncated = _diagnostics(omissions)
    report["errors"] = error_values
    report["omissions"] = omission_values
    report["diagnostics_truncated"] = errors_truncated or omissions_truncated
    if report["completeness"] == "complete":
        if error_values:
            report["completeness"] = "conflicted"
        elif omission_values or report["diagnostics_truncated"]:
            report["completeness"] = "partial"
    return _bounded(report)
