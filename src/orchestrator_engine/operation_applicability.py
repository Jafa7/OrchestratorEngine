"""Producer-owned declaration binding for native local-check attempts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, core

DECLARATION_KIND = "ORCHESTRATOR_OPERATION_APPLICABILITY_DECLARATION"
ARTIFACT_KIND = "ORCHESTRATOR_OPERATION_APPLICABILITY"
ARTIFACT_VERSION = 1
MAX_INPUT_BYTES = 64 * 1024
MAX_STRING_BYTES = 256
MAX_CRITERIA = 64
MAX_DEPTH = 8
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z", re.ASCII)
OPERATION_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII
)
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z",
    re.ASCII,
)
TIME_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)\Z",
    re.ASCII,
)


class ApplicabilityError(RuntimeError):
    """A declaration or retained applicability graph is invalid."""


def admission_lock(project_root: Path, check_id: str, *, state_dir: str) -> Path:
    return (
        core.state_root(project_root, state_dir=state_dir)
        / "applicability-admissions"
        / f"{check_id}.lock"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant: {value}")


def _bounded_structure(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ApplicabilityError("applicability declaration exceeds nesting limit")
    if isinstance(value, dict):
        if len(value) > 32:
            raise ApplicabilityError("applicability declaration has too many fields")
        for key, item in value.items():
            _bounded_text(key, "field name")
            _bounded_structure(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_CRITERIA:
            raise ApplicabilityError("applicability declaration list is too large")
        for item in value:
            _bounded_structure(item, depth=depth + 1)
    elif isinstance(value, str):
        _bounded_text(value, "value")
    elif value is None or type(value) is bool or type(value) is int:
        return
    else:
        raise ApplicabilityError("applicability declaration contains invalid JSON")


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ApplicabilityError(f"{field} must be a nonempty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise ApplicabilityError(f"{field} must be valid UTF-8") from error
    if len(encoded) > MAX_STRING_BYTES or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
    ):
        raise ApplicabilityError(f"{field} must be a bounded printable string")
    return value


def _opaque_id(value: Any, field: str) -> str:
    value = _bounded_text(value, field)
    if not ID_PATTERN.fullmatch(value):
        raise ApplicabilityError(f"{field} is not a bounded opaque identifier")
    return value


def _utc_time(value: Any, field: str) -> str:
    value = _bounded_text(value, field)
    if len(value) > 40 or not TIME_PATTERN.fullmatch(value):
        raise ApplicabilityError(f"{field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApplicabilityError(f"{field} must be a UTC timestamp") from error
    if (
        parsed.tzinfo is None
        or parsed.astimezone(UTC).utcoffset() != UTC.utcoffset(None)
    ):
        raise ApplicabilityError(f"{field} must be a UTC timestamp")
    return value


def _secure_read(
    path: Path,
    *,
    maximum: int = MAX_INPUT_BYTES,
    within: Path | None = None,
) -> bytes:
    try:
        if within is not None and not path.resolve().is_relative_to(within.resolve()):
            raise ApplicabilityError("applicability path escapes the state root")
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ApplicabilityError("applicability input must be a regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise ApplicabilityError("applicability input changed before open")
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ApplicabilityError("applicability input exceeds the size limit")
        return raw
    except FileNotFoundError as error:
        raise ApplicabilityError(f"applicability input is missing: {path}") from error
    except OSError as error:
        raise ApplicabilityError(
            f"applicability input is unreadable: {path}"
        ) from error


def _parse_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ApplicabilityError(
            "applicability input is not strict UTF-8 JSON"
        ) from error
    _bounded_structure(value)
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _retry(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    fields = {
        "operation_kind",
        "operation_id",
        "attempt_id",
        "applicability_sha256",
        "source_namespace",
        "native_location_binding",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ApplicabilityError("retry_of must be an exact predecessor reference")
    if value.get("operation_kind") != "check":
        raise ApplicabilityError("retry predecessor must be a local check")
    normalized = {
        key: _bounded_text(value.get(key), f"retry_of.{key}") for key in fields
    }
    if not OPERATION_ID_PATTERN.fullmatch(normalized["operation_id"]):
        raise ApplicabilityError("retry predecessor operation id is invalid")
    if not UUID_PATTERN.fullmatch(normalized["attempt_id"]):
        raise ApplicabilityError("retry predecessor attempt id is invalid")
    for field in (
        "applicability_sha256",
        "source_namespace",
        "native_location_binding",
    ):
        if not DIGEST_PATTERN.fullmatch(normalized[field]):
            raise ApplicabilityError(f"retry predecessor {field} is invalid")
    return normalized


def normalize_declaration(value: Any) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "project",
        "work",
        "revision",
        "candidate",
        "criteria",
        "retry_of",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ApplicabilityError("unsupported applicability declaration fields")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("kind") != DECLARATION_KIND
    ):
        raise ApplicabilityError("unsupported applicability declaration version")
    criteria = value.get("criteria")
    if not isinstance(criteria, list) or not criteria or len(criteria) > MAX_CRITERIA:
        raise ApplicabilityError("criteria must be a nonempty bounded array")
    normalized_criteria = []
    seen: set[str] = set()
    for item in criteria:
        if not isinstance(item, dict) or set(item) != {"id", "revision"}:
            raise ApplicabilityError("criterion must contain id and revision")
        criterion_id = _opaque_id(item.get("id"), "criterion id")
        if criterion_id in seen:
            raise ApplicabilityError("criterion identifiers must be unique")
        seen.add(criterion_id)
        normalized_criteria.append(
            {
                "id": criterion_id,
                "revision": _opaque_id(item.get("revision"), "criterion revision"),
            }
        )
    return {
        "schema_version": 1,
        "kind": DECLARATION_KIND,
        "project": _opaque_id(value.get("project"), "project"),
        "work": _opaque_id(value.get("work"), "work"),
        "revision": _opaque_id(value.get("revision"), "revision"),
        "candidate": _opaque_id(value.get("candidate"), "candidate"),
        "criteria": normalized_criteria,
        "retry_of": _retry(value.get("retry_of")),
    }


def read_declaration(path: Path) -> tuple[bytes, dict[str, Any], str]:
    raw = _secure_read(path.expanduser())
    declaration = normalize_declaration(_parse_json(raw))
    return raw, declaration, _digest(declaration)


def source_binding(
    project_root: Path, *, state_dir: str
) -> dict[str, str | int]:
    project = project_root.expanduser().resolve()
    state = core.state_root(project, state_dir=state_dir).resolve()

    def native(value: Path) -> str:
        return os.path.normcase(str(value))

    project_id = core.project_id(project)
    namespace = _digest(
        {
            "domain": "orchestrator-engine/native-source/v1",
            "platform": sys.platform,
            "host": platform.node().casefold(),
        }
    )
    location = _digest(
        {
            "domain": "orchestrator-engine/native-location/v1",
            "project_root": native(project),
            "state_root": native(state),
            "project_id": project_id,
        }
    )
    return {
        "binding_version": 1,
        "project_id": project_id,
        "source_namespace": namespace,
        "native_location_binding": location,
    }


def _atomic_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    core.atomic_replace(temporary, path)


def claim_json(path: Path, value: Any) -> bool:
    """Publish complete JSON only when the destination does not exist."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    raw = core.json_text(value).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _artifact_paths(directory: Path) -> tuple[Path, Path]:
    return directory / "applicability-input.json", directory / "applicability.json"


def _validate_predecessor(
    project_root: Path,
    *,
    state_dir: str,
    declaration: dict[str, Any],
    source: dict[str, Any],
    require_current_source: bool = True,
) -> dict[str, str] | None:
    retry = declaration["retry_of"]
    if retry is None:
        return None
    if (
        retry["source_namespace"] != source["source_namespace"]
        or retry["native_location_binding"] != source["native_location_binding"]
    ):
        raise ApplicabilityError("retry predecessor belongs to a different source")
    directory = (
        core.state_root(project_root, state_dir=state_dir)
        / "checks"
        / retry["operation_id"]
    )
    from . import operation_evidence

    capture: dict[str, Any] = {}
    report = operation_evidence.operation_evidence(
        project_root,
        target=f"check:{retry['operation_id']}",
        state_dir=state_dir,
        _capture=capture,
    )
    descriptor = capture.get("descriptor")
    if descriptor is None:
        raise ApplicabilityError("retry predecessor descriptor is invalid")
    if report["operation"]["terminal"] is not True:
        raise ApplicabilityError("retry predecessor is not terminal")
    if (
        report["completeness"] != "complete"
        or report["snapshot"]["consistency"] != "stable"
        or report.get("errors")
    ):
        raise ApplicabilityError("retry predecessor terminal graph is invalid")
    fence = operation_evidence._Reader(
        project_root.expanduser().resolve(),
        core.state_root(project_root, state_dir=state_dir).resolve(),
        operation_evidence.MAX_READ_BYTES,
    )
    retained_files = [
        (
            capture.get("descriptor_path"),
            capture.get("descriptor_raw"),
            capture.get("descriptor_presence"),
            False,
        ),
        (
            capture.get("owner_path"),
            capture.get("owner_raw"),
            capture.get("owner_presence"),
            True,
        ),
        *(
            (
                capture.get("paths", {}).get(role),
                capture.get("raw", {}).get(role),
                capture.get("presences", {}).get(role),
                False,
            )
            for role in operation_evidence.ROLES
        ),
    ]
    for path, retained_raw, retained_presence, optional in retained_files:
        current_raw, current_presence = fence.read(path, None, optional=optional)
        if (
            current_raw != retained_raw
            or current_presence != retained_presence
            or fence.errors
            or fence.omissions
        ):
            raise ApplicabilityError("retry predecessor terminal graph changed")
    artifact = validate_artifact(
        project_root,
        state_dir=state_dir,
        operation_id=retry["operation_id"],
        directory=directory,
        descriptor=descriptor,
        require_current_source=require_current_source,
        validate_retry=False,
    )
    validate_predecessor_observation(
        declaration=declaration,
        source=source,
        retry=retry,
        report=report,
        descriptor=descriptor,
        artifact=artifact,
        artifact_sha256=descriptor["applicability"]["artifact_sha256"],
        objects=capture.get("objects", {}),
    )
    return retry


def validate_predecessor_observation(
    *,
    declaration: dict[str, Any],
    source: dict[str, Any],
    retry: dict[str, str],
    report: dict[str, Any],
    descriptor: dict[str, Any],
    artifact: dict[str, Any],
    artifact_sha256: str,
    objects: dict[str, dict[str, Any] | None],
) -> None:
    """Validate one already bounded, sealed predecessor observation."""

    if report.get("operation", {}).get("terminal") is not True:
        raise ApplicabilityError("retry predecessor is not terminal")
    if (
        report.get("completeness") != "complete"
        or report.get("snapshot", {}).get("consistency") != "stable"
        or report.get("errors")
    ):
        raise ApplicabilityError("retry predecessor terminal graph is invalid")
    if artifact_sha256 != retry["applicability_sha256"]:
        raise ApplicabilityError("retry predecessor applicability digest mismatch")
    if artifact["attempt_id"] != retry["attempt_id"]:
        raise ApplicabilityError("retry predecessor attempt mismatch")
    if (
        artifact["source"]["source_namespace"] != retry["source_namespace"]
        or artifact["source"]["native_location_binding"]
        != retry["native_location_binding"]
        or artifact["source"] != source
    ):
        raise ApplicabilityError("retry predecessor belongs to a different source")
    metadata = descriptor.get("applicability")
    if not _metadata_shape(metadata):
        raise ApplicabilityError("retry predecessor applicability binding is invalid")
    for role in ("result", "evidence"):
        terminal = objects.get(role)
        if terminal is None:
            raise ApplicabilityError("retry predecessor terminal binding is missing")
        if not metadata_matches(terminal.get("applicability"), metadata):
            raise ApplicabilityError("retry predecessor terminal binding mismatch")
    previous = artifact["declaration"]
    if (
        previous["project"] != declaration["project"]
        or previous["work"] != declaration["work"]
    ):
        raise ApplicabilityError("retry predecessor logical work mismatch")


def _metadata(
    artifact_path: Path, input_path: Path, artifact: dict[str, Any]
) -> dict[str, Any]:
    return _metadata_from_raw(
        artifact_path,
        input_path,
        artifact,
        raw_artifact=_secure_read(artifact_path),
    )


def _metadata_from_raw(
    artifact_path: Path,
    input_path: Path,
    artifact: dict[str, Any],
    *,
    raw_artifact: bytes,
) -> dict[str, Any]:
    return {
        "producer_version": ARTIFACT_VERSION,
        "attempt_id": artifact["attempt_id"],
        "artifact_path": artifact_path.name,
        "artifact_sha256": hashlib.sha256(raw_artifact).hexdigest(),
        "input_path": input_path.name,
        "input_raw_sha256": artifact["input_raw_sha256"],
        "declaration_sha256": artifact["declaration_sha256"],
        "request_fingerprint": artifact["request_fingerprint"],
        "source_namespace": artifact["source"]["source_namespace"],
        "native_location_binding": artifact["source"]["native_location_binding"],
        "suite_fingerprint": artifact["suite_fingerprint"],
    }


def prepare(
    project_root: Path,
    *,
    state_dir: str,
    operation_id: str,
    directory: Path,
    suite_fingerprint: str,
    declaration_path: Path,
) -> dict[str, Any]:
    raw, declaration, declaration_sha = read_declaration(declaration_path)
    source = source_binding(project_root, state_dir=state_dir)
    retry = _validate_predecessor(
        project_root,
        state_dir=state_dir,
        declaration=declaration,
        source=source,
    )
    request_fingerprint = _digest(
        {
            "declaration_sha256": declaration_sha,
            "retry_of": retry,
            "suite_fingerprint": suite_fingerprint,
            "source": source,
        }
    )
    input_path, artifact_path = _artifact_paths(directory)
    if input_path.exists() != artifact_path.exists():
        raise ApplicabilityError(
            "incomplete applicability publication requires explicit recovery"
        )
    if artifact_path.exists():
        artifact = validate_artifact(
            project_root,
            state_dir=state_dir,
            operation_id=operation_id,
            directory=directory,
        )
        if artifact["request_fingerprint"] != request_fingerprint:
            raise ApplicabilityError(
                "check already has a different applicability declaration or retry"
            )
        return _metadata(artifact_path, input_path, artifact)
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(input_path, raw)
    artifact = {
        "schema_version": 1,
        "kind": ARTIFACT_KIND,
        "applicability_version": ARTIFACT_VERSION,
        "attempt_id": str(uuid.uuid4()),
        "captured_at": core.utc_now(),
        "operation_kind": "check",
        "operation_id": operation_id,
        "source": source,
        "producer": {
            "engine_version": __version__,
            "python_version": platform.python_version(),
        },
        "suite_fingerprint": suite_fingerprint,
        "input_raw_sha256": hashlib.sha256(raw).hexdigest(),
        "declaration_sha256": declaration_sha,
        "declaration": declaration,
        "retry_of": retry,
        "request_fingerprint": request_fingerprint,
        "assurance": {
            "declaration_binding": "retained",
            "executed_candidate": "unknown",
            "criteria_fulfillment": "unknown",
        },
    }
    core.atomic_json(artifact_path, artifact)
    return _metadata(artifact_path, input_path, artifact)


def _metadata_shape(value: Any) -> bool:
    required = {
        "producer_version",
        "attempt_id",
        "artifact_path",
        "artifact_sha256",
        "input_path",
        "input_raw_sha256",
        "declaration_sha256",
        "request_fingerprint",
        "source_namespace",
        "native_location_binding",
        "suite_fingerprint",
    }
    if not isinstance(value, dict) or set(value) != required:
        return False
    if type(value.get("producer_version")) is not int or value.get(
        "producer_version"
    ) != ARTIFACT_VERSION:
        return False
    if not isinstance(value.get("attempt_id"), str) or not UUID_PATTERN.fullmatch(
        value["attempt_id"]
    ):
        return False
    if value.get("artifact_path") != "applicability.json" or value.get(
        "input_path"
    ) != "applicability-input.json":
        return False
    return all(
        isinstance(value.get(field), str) and DIGEST_PATTERN.fullmatch(value[field])
        for field in required
        - {"producer_version", "attempt_id", "artifact_path", "input_path"}
    )


def metadata_matches(value: Any, expected: Any) -> bool:
    """Compare copied applicability metadata with exact JSON types."""

    if expected is None:
        return value is None
    return _metadata_shape(value) and _metadata_shape(expected) and value == expected


def validate_artifact(
    project_root: Path,
    *,
    state_dir: str,
    operation_id: str,
    directory: Path,
    descriptor: dict[str, Any] | None = None,
    require_current_source: bool = True,
    validate_retry: bool = True,
    raw_input: bytes | None = None,
    raw_artifact: bytes | None = None,
) -> dict[str, Any]:
    input_path, artifact_path = _artifact_paths(directory)
    if (raw_input is None) != (raw_artifact is None):
        raise ApplicabilityError("applicability byte observation is incomplete")
    if raw_input is None or raw_artifact is None:
        state = core.state_root(project_root, state_dir=state_dir).resolve()
        raw_input = _secure_read(input_path, within=state)
        raw_artifact = _secure_read(artifact_path, within=state)
    artifact = _parse_json(raw_artifact)
    fields = {
        "schema_version",
        "kind",
        "applicability_version",
        "attempt_id",
        "captured_at",
        "operation_kind",
        "operation_id",
        "source",
        "producer",
        "suite_fingerprint",
        "input_raw_sha256",
        "declaration_sha256",
        "declaration",
        "retry_of",
        "request_fingerprint",
        "assurance",
    }
    if not isinstance(artifact, dict) or set(artifact) != fields:
        raise ApplicabilityError("unsupported applicability artifact fields")
    if (
        type(artifact.get("schema_version")) is not int
        or artifact.get("schema_version") != 1
        or artifact.get("kind") != ARTIFACT_KIND
        or type(artifact.get("applicability_version")) is not int
        or artifact.get("applicability_version") != ARTIFACT_VERSION
    ):
        raise ApplicabilityError("unsupported applicability artifact version")
    if artifact.get("operation_kind") != "check" or artifact.get(
        "operation_id"
    ) != operation_id:
        raise ApplicabilityError("applicability operation identity mismatch")
    if not UUID_PATTERN.fullmatch(str(artifact.get("attempt_id", ""))):
        raise ApplicabilityError("applicability attempt identity is invalid")
    _utc_time(artifact.get("captured_at"), "captured_at")
    declaration = normalize_declaration(artifact.get("declaration"))
    if artifact.get("retry_of") != declaration["retry_of"]:
        raise ApplicabilityError("applicability retry binding mismatch")
    retained_source = artifact.get("source")
    if not isinstance(retained_source, dict) or set(retained_source) != {
        "binding_version",
        "project_id",
        "source_namespace",
        "native_location_binding",
    }:
        raise ApplicabilityError("applicability source binding is invalid")
    if type(retained_source.get("binding_version")) is not int or retained_source.get(
        "binding_version"
    ) != 1:
        raise ApplicabilityError("applicability source binding is invalid")
    _bounded_text(retained_source.get("project_id"), "source.project_id")
    if any(
        not isinstance(retained_source.get(field), str)
        or not DIGEST_PATTERN.fullmatch(retained_source[field])
        for field in ("source_namespace", "native_location_binding")
    ):
        raise ApplicabilityError("applicability source binding is invalid")
    source = source_binding(project_root, state_dir=state_dir)
    if require_current_source and retained_source != source:
        raise ApplicabilityError("applicability native location binding mismatch")
    producer = artifact.get("producer")
    if not isinstance(producer, dict) or set(producer) != {
        "engine_version",
        "python_version",
    }:
        raise ApplicabilityError("applicability producer runtime is invalid")
    _bounded_text(producer.get("engine_version"), "producer.engine_version")
    _bounded_text(producer.get("python_version"), "producer.python_version")
    assurance = artifact.get("assurance")
    if assurance != {
        "declaration_binding": "retained",
        "executed_candidate": "unknown",
        "criteria_fulfillment": "unknown",
    }:
        raise ApplicabilityError("applicability assurance state is invalid")
    digest_fields = (
        "suite_fingerprint",
        "input_raw_sha256",
        "declaration_sha256",
        "request_fingerprint",
    )
    if any(
        not isinstance(artifact.get(field), str)
        or not DIGEST_PATTERN.fullmatch(artifact[field])
        for field in digest_fields
    ):
        raise ApplicabilityError("applicability digest field is invalid")
    if artifact["input_raw_sha256"] != hashlib.sha256(raw_input).hexdigest():
        raise ApplicabilityError("retained applicability input digest mismatch")
    parsed_input = normalize_declaration(_parse_json(raw_input))
    if parsed_input != declaration or artifact["declaration_sha256"] != _digest(
        declaration
    ):
        raise ApplicabilityError("retained applicability declaration mismatch")
    expected_request = _digest(
        {
            "declaration_sha256": artifact["declaration_sha256"],
            "retry_of": artifact["retry_of"],
            "suite_fingerprint": artifact["suite_fingerprint"],
            "source": artifact["source"],
        }
    )
    if artifact["request_fingerprint"] != expected_request:
        raise ApplicabilityError("applicability request fingerprint mismatch")
    if validate_retry:
        verified_retry = _validate_predecessor(
            project_root,
            state_dir=state_dir,
            declaration=declaration,
            source=retained_source,
            require_current_source=require_current_source,
        )
        if verified_retry != artifact["retry_of"]:
            raise ApplicabilityError("applicability retry graph mismatch")
    if descriptor is not None:
        metadata = descriptor.get("applicability")
        if not _metadata_shape(metadata):
            raise ApplicabilityError("descriptor applicability metadata is invalid")
        expected = _metadata_from_raw(
            artifact_path,
            input_path,
            artifact,
            raw_artifact=raw_artifact,
        )
        if metadata != expected:
            raise ApplicabilityError("descriptor applicability binding mismatch")
        if descriptor.get("fingerprint") != artifact["suite_fingerprint"]:
            raise ApplicabilityError("applicability suite fingerprint mismatch")
    return artifact


def replay_matches(
    project_root: Path,
    *,
    state_dir: str,
    descriptor: dict[str, Any],
    declaration_path: Path | None,
) -> bool:
    metadata = descriptor.get("applicability")
    directory = Path(descriptor["check_dir"])
    if declaration_path is None:
        input_path, artifact_path = _artifact_paths(directory)
        return (
            metadata is None
            and not input_path.exists()
            and not artifact_path.exists()
        )
    if not _metadata_shape(metadata):
        return False
    _, declaration, declaration_sha = read_declaration(declaration_path)
    if declaration_sha != metadata["declaration_sha256"]:
        return False
    artifact = validate_artifact(
        project_root,
        state_dir=state_dir,
        operation_id=descriptor["check_id"],
        directory=directory,
        descriptor=descriptor,
    )
    return artifact["declaration"] == declaration


def validate_descriptor_binding(
    project_root: Path,
    *,
    state_dir: str,
    descriptor: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate a committed descriptor before any command starts."""

    operation_id = str(descriptor["check_id"])
    expected_directory = (
        core.state_root(project_root, state_dir=state_dir)
        / "checks"
        / operation_id
    ).resolve()
    directory = Path(str(descriptor["check_dir"])).resolve()
    if directory != expected_directory:
        raise ApplicabilityError("applicability check directory binding mismatch")
    input_path, artifact_path = _artifact_paths(directory)
    if descriptor.get("applicability") is None:
        if input_path.exists() or artifact_path.exists():
            raise ApplicabilityError(
                "unbound applicability preparation requires explicit recovery"
            )
        return None
    return validate_artifact(
        project_root,
        state_dir=state_dir,
        operation_id=operation_id,
        directory=directory,
        descriptor=descriptor,
    )


def terminal_binding(descriptor: dict[str, Any]) -> dict[str, Any] | None:
    metadata = descriptor.get("applicability")
    return json.loads(json.dumps(metadata)) if _metadata_shape(metadata) else None
