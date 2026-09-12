"""Loopback transport and native authority for cooperative resource recipes."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from socketserver import TCPServer

from . import core, platform_runtime, worker_lease
from .resource_queue import (
    OWNING,
    Ledger,
    ResourceError,
    canonical,
    digest,
    identifier,
    normalize_registry,
    validate_subscriber,
)

RESOURCE_INPUT_CONTRACT_KIND = "ORCHESTRATOR_RESOURCE_INPUT_CONTRACT"
RESOURCE_INPUT_CONTRACT_SCHEMA_VERSION = 1
_RESOURCE_INPUT_CONTRACT_FIELDS = {
    "schema_version",
    "kind",
    "request_id",
    "recipe",
    "recipe_digest",
    "lineage",
    "inputs",
    "input_contract_digest",
}


def local_directory(path):
    path = Path(path).expanduser().resolve()
    if str(path).startswith(("\\\\", "//")):
        raise ResourceError("authority storage must use a native local filesystem")
    if sys.platform == "linux":
        mounts = Path("/proc/mounts").read_text().splitlines()
        matches = [
            line.split()
            for line in mounts
            if path.is_relative_to(line.split()[1].replace("\\040", " "))
        ]
        if matches and max(matches, key=lambda m: len(m[1]))[2] in {
            "9p",
            "drvfs",
            "cifs",
            "nfs",
            "nfs4",
            "smbfs",
            "fuse.sshfs",
        }:
            raise ResourceError(
                "authority ledger cannot reside on a shared/foreign mount"
            )
    if path.exists():
        _require_private_directory(path)
    return path.resolve()


def _require_private_directory(path):
    if not path.is_dir():
        raise ResourceError("authority storage must be a directory")
    if os.name == "nt":
        return
    metadata = path.stat()
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise ResourceError("authority directory must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ResourceError("authority directory must use mode 0700")


def _private_atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            handle.write(core.json_text(value))
        core.atomic_replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def command_contract(command):
    if not isinstance(command, dict):
        raise ResourceError("command must be an object")
    argv = command.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(v, str) and v and "\x00" not in v for v in argv)
    ):
        raise ResourceError("command requires a nonempty string argv")
    cwd = command.get("cwd", ".")
    if not isinstance(cwd, str) or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
        raise ResourceError("command cwd must stay inside its input workspace")
    timeout = command.get("timeout_seconds")
    if timeout is not None and (
        type(timeout) not in {int, float} or not 0 < timeout < float("inf")
    ):
        raise ResourceError("timeout must be a positive finite number or omitted")


def validate_config(config):
    projects = config.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise ResourceError("register at least one project")
    for name, project in projects.items():
        identifier(name)
        root = Path(project["root"]).resolve(strict=True)
        if not root.is_dir():
            raise ResourceError("project root must be a directory")
        for recipe in project["recipes"].values():
            if not isinstance(recipe.get("inputs"), list) or not recipe["inputs"]:
                raise ResourceError("recipe must declare explicit input paths")
            for stage in recipe["stages"]:
                commands = stage.get("commands")
                if not isinstance(commands, list) or not commands:
                    raise ResourceError("stage must contain commands")
                for command in commands + stage.get("cleanup", []):
                    command_contract(command)
    for resource in config["resources"].values():
        if resource.get("release") == "probe":
            command_contract(resource["probe"])
            if resource["probe"].get("timeout_seconds") is None:
                raise ResourceError("quiescence probe requires an explicit timeout")


def initialize(directory, config):
    directory = local_directory(directory)
    validate_config(config)
    if (directory / "config.json").exists() or (
        directory / "resources.sqlite3"
    ).exists():
        raise ResourceError(
            "authority already exists; refusing to replace identity or ownership"
        )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_private_directory(directory)
    with contextlib.closing(Ledger(directory, create=True)) as ledger:
        ledger.configure(config["resources"])
        config = json.loads(canonical(config))
        config["authority"] = ledger.get("authority")
        config["revision"] = 1
        config["resource_digest"] = digest(ledger.get("registry"))
        for project in config["projects"].values():
            project["token"] = secrets.token_urlsafe(32)
            project["root"] = str(Path(project["root"]).resolve())
        _private_atomic_json(directory / "config.json", config)
    return {
        "authority": config["authority"],
        "directory": str(directory),
        "revision": config["revision"],
        "resource_digest": config["resource_digest"],
    }


def update_configuration(directory, config, *, expected_revision):
    """Replace a drained authority configuration without replacing its identity."""
    directory = local_directory(directory)
    validate_config(config)
    if type(expected_revision) is not int or expected_revision < 1:
        raise ResourceError("expected revision must be a positive integer")
    with (
        platform_runtime.exclusive_file_lock(
            directory / "service.lock", timeout_seconds=0
        ),
        platform_runtime.exclusive_file_lock(directory / "configuration.lock"),
    ):
        _reconcile_configuration_unlocked(directory)
        current = core.load_object(directory / "config.json")
        revision = current.get("revision", 1)
        if revision != expected_revision:
            raise ResourceError(
                f"configuration revision changed: expected {expected_revision}, "
                f"found {revision}"
            )
        with contextlib.closing(Ledger(directory)) as ledger:
            if ledger.get("authority") != current.get("authority"):
                raise ResourceError(
                    "authority identity mismatch; reconcile restored state"
                )
            old_registry = ledger.get("registry")
            recorded_digest = current.get("resource_digest")
            if recorded_digest is not None and recorded_digest != digest(old_registry):
                raise ResourceError(
                    "authority configuration does not match the resource ledger"
                )
            active = ledger.stages(active=True)
            if active:
                raise ResourceError(
                    "configuration update requires a stopped, fully drained authority"
                )
            updated = json.loads(canonical(config))
            for name, registered in current["projects"].items():
                replacement = updated["projects"].get(name)
                if replacement is None:
                    raise ResourceError("configuration update cannot remove projects")
                if Path(replacement["root"]).resolve() != Path(registered["root"]):
                    raise ResourceError(
                        "configuration update cannot change an existing project root"
                    )
                replacement["token"] = registered["token"]
            for name, project in updated["projects"].items():
                project["root"] = str(Path(project["root"]).resolve())
                if name not in current["projects"]:
                    project["token"] = secrets.token_urlsafe(32)
            registry = normalize_registry(updated["resources"])
            updated.update(
                authority=current["authority"],
                revision=revision + 1,
                resource_digest=digest(registry),
            )
            staged = directory / "config.next.json"
            _private_atomic_json(staged, updated)
            try:
                ledger.configure(updated["resources"])
                core.atomic_replace(staged, directory / "config.json")
            except BaseException:
                if ledger.get("registry") != old_registry:
                    ledger.configure(old_registry)
                raise
            finally:
                with contextlib.suppress(OSError):
                    staged.unlink()
    return {
        "authority": updated["authority"],
        "directory": str(directory),
        "revision": updated["revision"],
        "resource_digest": updated["resource_digest"],
    }


def _reconcile_configuration_unlocked(directory):
    """Finish or discard one recognized interrupted configuration update."""

    staged = directory / "config.next.json"
    if not staged.is_file():
        return "unchanged"
    current = core.load_object(directory / "config.json")
    candidate = core.load_object(staged)
    validate_config(candidate)
    if (
        candidate.get("authority") != current.get("authority")
        or candidate.get("revision") != current.get("revision", 1) + 1
    ):
        raise ResourceError("staged configuration is not a recognized next revision")
    with contextlib.closing(Ledger(directory)) as ledger:
        registry_digest = digest(ledger.get("registry"))
    if registry_digest == candidate.get("resource_digest"):
        core.atomic_replace(staged, directory / "config.json")
        return "committed"
    if registry_digest == current.get("resource_digest"):
        staged.unlink()
        return "rolled_back"
    raise ResourceError(
        "staged configuration and resource ledger have unrelated revisions"
    )


def _reconcile_configuration(directory):
    with platform_runtime.exclusive_file_lock(directory / "configuration.lock"):
        return _reconcile_configuration_unlocked(directory)


def input_manifest(root, inputs):
    root = Path(root).resolve()
    files = {}
    for relative in inputs:
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ResourceError("input path must be relative")
        path = root / relative
        if not path.resolve().is_relative_to(root) or ".." in Path(relative).parts:
            raise ResourceError("input path escapes project")
        if not path.exists():
            raise ResourceError(f"missing recipe input: {relative}")
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
                raise ResourceError("snapshot inputs must not contain symbolic links")
            if candidate.is_file():
                key = candidate.relative_to(root).as_posix()
                if ".orchestrator" in candidate.relative_to(root).parts:
                    raise ResourceError(
                        "do not include orchestration state in recipe inputs"
                    )
                files[key] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if not files:
        raise ResourceError("recipe inputs contain no files")
    return files


def validate_input_manifest(value):
    """Validate one retained manifest without reading its source files."""

    if not isinstance(value, dict) or not value:
        raise ResourceError("input contract requires a nonempty input manifest")
    normalized = {}
    for relative, sha256 in value.items():
        if not isinstance(relative, str) or not relative or len(relative) > 4096:
            raise ResourceError("input manifest paths must be nonempty strings")
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or path.as_posix() != relative
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in path.parts)
            or ".orchestrator" in path.parts
        ):
            raise ResourceError("input manifest path must stay inside project inputs")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ResourceError(
                "input manifest values must be lowercase SHA-256 hashes"
            )
        normalized[relative] = sha256
    return normalized


def _input_contract_body(*, recipe, recipe_digest, request_id, lineage, inputs):
    identifier(recipe)
    identifier(request_id)
    if lineage is not None:
        identifier(lineage)
    if (
        not isinstance(recipe_digest, str)
        or len(recipe_digest) != 64
        or any(character not in "0123456789abcdef" for character in recipe_digest)
    ):
        raise ResourceError("recipe digest must be a lowercase SHA-256 hash")
    return {
        "request_id": request_id,
        "recipe": recipe,
        "recipe_digest": recipe_digest,
        "lineage": lineage,
        "inputs": validate_input_manifest(inputs),
    }


def build_input_contract(*, recipe, recipe_digest, request_id, lineage=None, inputs):
    """Build a portable immutable contract for a later resource submission."""

    body = _input_contract_body(
        recipe=recipe,
        recipe_digest=recipe_digest,
        request_id=request_id,
        lineage=lineage,
        inputs=inputs,
    )
    return {
        "schema_version": RESOURCE_INPUT_CONTRACT_SCHEMA_VERSION,
        "kind": RESOURCE_INPUT_CONTRACT_KIND,
        **body,
        "input_contract_digest": digest(body),
    }


def validate_input_contract(value):
    """Return a normalized retained contract or reject unsupported/tampered data."""

    if not isinstance(value, dict) or set(value) != _RESOURCE_INPUT_CONTRACT_FIELDS:
        raise ResourceError("unsupported resource input contract fields")
    if value.get("schema_version") != RESOURCE_INPUT_CONTRACT_SCHEMA_VERSION:
        raise ResourceError("unsupported resource input contract schema version")
    if value.get("kind") != RESOURCE_INPUT_CONTRACT_KIND:
        raise ResourceError("unsupported resource input contract kind")
    body = _input_contract_body(
        recipe=value.get("recipe"),
        recipe_digest=value.get("recipe_digest"),
        request_id=value.get("request_id"),
        lineage=value.get("lineage"),
        inputs=value.get("inputs"),
    )
    if value.get("input_contract_digest") != digest(body):
        raise ResourceError("resource input contract digest does not match its content")
    return {
        "schema_version": RESOURCE_INPUT_CONTRACT_SCHEMA_VERSION,
        "kind": RESOURCE_INPUT_CONTRACT_KIND,
        **body,
        "input_contract_digest": digest(body),
    }


def capture(root, destination, expected):
    destination.mkdir(parents=True)
    try:
        for relative, sha in expected.items():
            source = Path(root) / relative
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                raise ResourceError(
                    "input changed during snapshot capture; resubmit explicitly"
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            shutil.copymode(source, target)
    except BaseException:
        # Only the unique, private, not-yet-submitted snapshot is removed.
        shutil.rmtree(destination)
        raise


class Authority:
    def __init__(self, directory):
        self.directory = local_directory(directory)
        _reconcile_configuration(self.directory)
        self.config = core.load_object(self.directory / "config.json")
        self.lock = threading.RLock()
        self.children = {}
        with contextlib.closing(Ledger(self.directory)) as ledger:
            if ledger.get("authority") != self.config["authority"]:
                raise ResourceError(
                    "authority identity mismatch; reconcile restored state"
                )
            expected_registry = self.config.get("resource_digest")
            if expected_registry is not None and expected_registry != digest(
                ledger.get("registry")
            ):
                raise ResourceError(
                    "authority configuration does not match the resource ledger"
                )

    def authenticate(self, project, token):
        registered = self.config["projects"].get(project)
        if not registered or not hmac.compare_digest(registered["token"], token):
            raise ResourceError("unregistered project or invalid credential")
        return registered

    def call(self, project, action, payload):
        registered = self.config["projects"][project]
        if action == "recipe":
            recipe = registered["recipes"].get(payload["recipe"])
            if recipe is None:
                raise ResourceError("recipe is not registered")
            return {"inputs": recipe["inputs"], "recipe_digest": digest(recipe)}
        if action == "submit":
            return self.submit(project, payload)
        with self.lock, contextlib.closing(Ledger(self.directory)) as ledger:
            request = payload.get("request")
            if request and ledger.request(request)["project"] != project:
                raise ResourceError("request belongs to another project")
            if action == "status":
                return ledger.snapshot(project, request)
            if action == "metrics":
                return ledger.metrics(project)
            if action == "context":
                stage = ledger.stage(payload["stage"])
                purpose = payload.get("purpose", "work")
                expected_token = (
                    stage.get("launch_token", "")
                    if purpose == "work"
                    else stage.get("maintenance_token", "")
                    if purpose == "maintenance"
                    else ""
                )
                supplied_token = payload.get("token")
                token_valid = (
                    isinstance(expected_token, str)
                    and bool(expected_token)
                    and isinstance(supplied_token, str)
                    and bool(supplied_token)
                    and hmac.compare_digest(expected_token, supplied_token)
                )
                if (
                    stage["project"] != project
                    or stage["request"] != request
                    or stage["state"] != "running"
                    or stage["epoch"] != payload.get("epoch")
                    or purpose not in {"work", "maintenance"}
                    or (purpose == "work" and stage.get("cancel_requested"))
                    or not token_valid
                ):
                    raise ResourceError("resource execution context is revoked")
                return {
                    "authority": ledger.get("authority"),
                    "valid": True,
                    "purpose": purpose,
                    "allocation": stage["allocation"],
                    "incarnations": stage["resource_incarnations"],
                }
            if action == "cancel":
                ledger.cancel(request)
            elif action in {"subscribe", "unsubscribe"}:
                ledger.subscribe(
                    request,
                    payload["contract_digest"],
                    payload["subscriber"],
                    remove=action == "unsubscribe",
                )
            else:
                raise ResourceError("unsupported project action")
            return ledger.snapshot(project, request)

    def submit(self, project, payload):
        registered = self.config["projects"][project]
        recipe = registered["recipes"].get(payload["recipe"])
        if recipe is None or digest(recipe) != payload["recipe_digest"]:
            raise ResourceError("recipe changed or is not registered")
        external = identifier(payload["id"])
        declared_inputs = validate_input_manifest(payload["inputs"])
        lineage = payload.get("lineage")
        if lineage is not None:
            identifier(lineage)
        subscriber = payload.get("subscriber")
        if subscriber is not None:
            validate_subscriber(subscriber)
        # Compare replay before inspecting live inputs: accepted inputs are immutable.
        contract = {
            "recipe": recipe,
            "inputs": declared_inputs,
            "lineage": lineage,
        }
        fingerprint = digest(contract)
        with self.lock, contextlib.closing(Ledger(self.directory)) as ledger:
            row = ledger.db.execute(
                "SELECT id FROM requests WHERE project=? AND external=?",
                (project, external),
            ).fetchone()
            if row:
                return self._replay_submission(
                    ledger,
                    row[0],
                    fingerprint,
                    subscriber,
                    conflict="request ID already has different immutable inputs",
                )
        actual = input_manifest(registered["root"], recipe["inputs"])
        if actual != declared_inputs:
            raise ResourceError(
                "declared input snapshot differs from registered project"
            )
        workspace = self.directory / "snapshots" / str(uuid.uuid4())
        capture(registered["root"], workspace, actual)
        plan = {
            "stages": recipe["stages"],
            "contract_digest": fingerprint,
            "input_manifest": actual,
            "workspace": str(workspace),
            "recipe_digest": digest(recipe),
            "lineage": lineage,
        }
        workspace_referenced = False
        try:
            with self.lock, contextlib.closing(Ledger(self.directory)) as ledger:
                # Concurrent replay may have won while this snapshot was captured.
                row = ledger.db.execute(
                    "SELECT id FROM requests WHERE project=? AND external=?",
                    (project, external),
                ).fetchone()
                if row:
                    replay = self._replay_submission(
                        ledger,
                        row[0],
                        fingerprint,
                        subscriber,
                        conflict="concurrent request ID conflict",
                    )
                else:
                    request = ledger.submit(project, external, plan, subscriber)
                    workspace_referenced = True
                    replay = None
        except BaseException:
            if not workspace_referenced and not self._snapshot_is_referenced(
                project, external, workspace
            ):
                with contextlib.suppress(OSError):
                    shutil.rmtree(workspace)
            raise
        if replay is not None:
            try:
                shutil.rmtree(workspace)
            except OSError as error:
                raise ResourceError(
                    "could not remove an unreferenced input snapshot"
                ) from error
            return replay
        return {
            "request": request,
            "contract_digest": digest(plan),
            "idempotent": False,
        }

    @staticmethod
    def _replay_submission(ledger, request, fingerprint, subscriber, *, conflict):
        saved = ledger.request(request)
        plan = saved["plan"]
        if plan["contract_digest"] != fingerprint:
            raise ResourceError(conflict)
        contract_digest = digest(plan)
        if subscriber is not None and subscriber not in saved["subscribers"]:
            ledger.subscribe(request, contract_digest, subscriber)
        return {
            "request": request,
            "contract_digest": contract_digest,
            "idempotent": True,
        }

    def _snapshot_is_referenced(self, project, external, workspace):
        try:
            with self.lock, contextlib.closing(Ledger(self.directory)) as ledger:
                row = ledger.db.execute(
                    "SELECT id FROM requests WHERE project=? AND external=?",
                    (project, external),
                ).fetchone()
                if row is None:
                    return False
                saved = ledger.request(row[0])["plan"].get("workspace")
                return Path(saved).resolve() == workspace.resolve()
        except Exception:
            # An unreadable ledger cannot prove that the snapshot is disposable.
            return True

    def reconcile(self, ledger):
        for stage in ledger.stages(active=True):
            if stage["state"] not in OWNING or stage["state"] == "recovery_required":
                continue
            process = self.children.get(stage["id"])
            if process is not None and stage["state"] == "granted":
                continue
            identity = stage.get("supervisor_identity")
            state = worker_lease.identity_state(identity)["state"]
            if state == "alive":
                continue
            # No timeout-based release, including a crash during spawn/attach.
            try:
                ledger.finish(
                    stage["id"],
                    stage["epoch"],
                    "failed",
                    [],
                    {"reason": "supervisor_uncertain", "identity_state": state},
                )
            except ResourceError:
                # The runner may have committed completion after our snapshot.
                if ledger.stage(stage["id"])["state"] in OWNING:
                    raise

    def launch(self, ledger, stage):
        directory = self.directory / "runs" / stage["id"]
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "supervisor.log").open("ab") as log:
            try:
                process = platform_runtime.spawn(
                    subprocess.Popen,
                    [
                        sys.executable,
                        "-m",
                        "orchestrator_engine.resource_runner",
                        str(self.directory),
                        stage["id"],
                    ],
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as error:
                ledger.finish(
                    stage["id"],
                    stage["epoch"],
                    "failed",
                    list(stage["allocation"]),
                    {"not_started": str(error)},
                )
                return
        self.children[stage["id"]] = process
        try:
            identity = worker_lease.process_identity(process.pid)
            if identity is None:
                raise ResourceError("supervisor identity unavailable")
            ledger.attach(stage["id"], stage["epoch"], identity)
            process.stdin.write((stage["launch_token"] + "\n").encode())
            process.stdin.flush()
        finally:
            process.stdin.close()

    def deliver(self, ledger):
        rows = ledger.db.execute(
            "SELECT id,body,attempts FROM outbox WHERE delivered=0 AND next_attempt<=?",
            (time.time(),),
        ).fetchall()
        for row in rows:
            value = json.loads(row[1])
            request = ledger.request(value["request"])
            if value["subscriber"] not in request["subscribers"]:
                ledger.db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (row[0],))
                continue
            stages = (
                value.get("stages")
                or ledger.snapshot(value["project"], value["request"])["stages"]
            )
            if value["status"] == "action_required" and not any(
                s["state"] == "recovery_required"
                for s in ledger.stages(value["request"])
            ):
                # Recovery supersedes an undelivered advisory, never a final result.
                ledger.db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (row[0],))
                continue
            project = Path(self.config["projects"][value["project"]]["root"])
            output = project / core.DEFAULT_STATE_DIR / "resources" / value["request"]
            try:
                for stage in stages:
                    evidence = stage.get("evidence", {})
                    if evidence.get("path") and core.sha256_file(
                        Path(evidence["path"])
                    ) != evidence.get("sha256"):
                        raise ResourceError(
                            "runner evidence is missing or inconsistent"
                        )
                output.mkdir(parents=True, exist_ok=True)
                result = output / (
                    f"recovery-required-{value['outcome_id']}.json"
                    if value["status"] == "action_required"
                    else "result.json"
                )
                # Retries must not rewrite evidence already delivered.
                if not result.exists():
                    public_value = {
                        key: item for key, item in value.items() if key != "subscriber"
                    }
                    core.atomic_json(
                        result,
                        {
                            **public_value,
                            "plan": request["plan"],
                            "stages": stages,
                        },
                    )
                event_path = core.event_path_for(project, row[0])
                subscriber = value["subscriber"]
                source_kind = "resource_recipe"
                operation_id = value["external"]
                if subscriber.get("check_id"):
                    from .resource_checks import project_result

                    result = project_result(
                        project,
                        subscriber,
                        value,
                        request,
                        stages,
                    )
                    source_kind, operation_id = "local_check", subscriber["check_id"]
                wake = (
                    subscriber.get("wake", False)
                    or subscriber.get("wake_policy") == "always"
                    or (
                        subscriber.get("wake_policy") == "on-failure"
                        and value["status"] != "passed"
                    )
                )
                if not event_path.exists():
                    core.write_followup_event(
                        project,
                        operation_id=operation_id,
                        source_kind=source_kind,
                        terminal_status="completed"
                        if value["status"] == "passed"
                        else value["status"],
                        result_path=result,
                        evidence_path=result,
                        event_id=row[0],
                        wake_target=value["subscriber"].get("wake_target"),
                        emit_signal=wake,
                    )
                elif wake and not core.signal_path_for(project, row[0]).exists():
                    # Recover a crash between event and signal writes using the
                    # same immutable identity; consumers deduplicate that ID.
                    event = core.verify_terminal_event(event_path)
                    signal = {
                        k: event[k]
                        for k in (
                            "schema_version",
                            "event_id",
                            "project_id",
                            "source_kind",
                            "operation_id",
                            "terminal_status",
                            "result_path",
                            "evidence_path",
                            "created_at",
                        )
                    }
                    signal.update(
                        kind="ORCHESTRATOR_FOLLOWUP_SIGNAL",
                        event_path=str(event_path),
                        requires="ORCHESTRATOR_FOLLOWUP",
                    )
                    if "wake_target" in event:
                        signal["wake_target"] = event["wake_target"]
                    core.atomic_json(core.signal_path_for(project, row[0]), signal)
                ledger.db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (row[0],))
            except (
                OSError,
                core.OrchestratorError,
                ValueError,
                TypeError,
                KeyError,
            ) as error:
                # Delivery is retriable independently of allocation release.
                ledger.event(row[0], "delivery_failed", error=str(error))
                attempts = row["attempts"] + 1
                ledger.db.execute(
                    "UPDATE outbox SET attempts=?,next_attempt=?,last_error=? "
                    "WHERE id=?",
                    (
                        attempts,
                        time.time() + min(60, 2 ** min(attempts, 6)),
                        str(error),
                        row[0],
                    ),
                )

    def deliver_pending(self, *, timeout_seconds=5):
        """Serialize terminal projection across the service and runner fallback."""

        with (
            platform_runtime.exclusive_file_lock(
                self.directory / "delivery.lock",
                timeout_seconds=timeout_seconds,
            ),
            contextlib.closing(Ledger(self.directory)) as ledger,
        ):
            self.deliver(ledger)

    def tick(self, *, deliver_results=True):
        with self.lock, contextlib.closing(Ledger(self.directory)) as ledger:
            self.reconcile(ledger)
            unavailable = [
                name
                for name, p in self.config["projects"].items()
                if not Path(p["root"]).is_dir()
            ]
            for stage in ledger.schedule(unavailable):
                try:
                    self.launch(ledger, stage)
                except (OSError, ResourceError) as error:
                    ledger.finish(
                        stage["id"],
                        stage["epoch"],
                        "failed",
                        [],
                        {"reason": "launch_uncertain", "error": str(error)},
                    )
            for stage_id, process in list(self.children.items()):
                if (
                    ledger.stage(stage_id)["state"] not in OWNING
                    and process.poll() is not None
                ):
                    del self.children[stage_id]
        if deliver_results:
            self.deliver_pending()


class LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer's reverse DNS lookup can stall offline/macOS startup.
        # The authenticated API binds an explicit numeric loopback address.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def serve(directory, *, port=0, stop=None, ready=None):
    platform_runtime.require_detached_lifecycle("resource authority")
    directory = local_directory(directory)
    with platform_runtime.exclusive_file_lock(
        directory / "service.lock", timeout_seconds=0
    ):
        authority = Authority(directory)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    if self.headers.get("Origin"):
                        raise ResourceError("browser-origin requests are not accepted")
                    project = self.headers.get("X-Project", "")
                    token = self.headers.get("Authorization", "").removeprefix(
                        "Bearer "
                    )
                    authority.authenticate(project, token)
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16 * 1024 * 1024:
                        raise ResourceError("invalid transport message length")
                    self.connection.settimeout(10)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ResourceError("request body must be an object")
                    result = authority.call(
                        project, self.path.removeprefix("/"), payload
                    )
                    status = 200
                except (
                    ResourceError,
                    KeyError,
                    ValueError,
                    TypeError,
                    OSError,
                ) as error:
                    result, status = {"error": str(error)}, 400
                data = canonical(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(data)

        identity = worker_lease.process_identity(os.getpid())
        if identity is None:
            raise ResourceError("resource authority process identity is unavailable")
        previous_endpoint = directory / "endpoint.json"
        if port == 0 and previous_endpoint.exists():
            port = int(core.load_object(previous_endpoint)["url"].rsplit(":", 1)[1])
        server = LoopbackHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True
        server.timeout = 0.1
        endpoint = {
            "url": f"http://127.0.0.1:{server.server_port}",
            "authority": authority.config["authority"],
            "identity": identity,
        }
        core.atomic_json(directory / "endpoint.json", endpoint)
        stop = stop or threading.Event()

        def dispatch():
            while not stop.is_set():
                with contextlib.suppress(platform_runtime.PlatformRuntimeError):
                    authority.deliver_pending(timeout_seconds=0.2)
                stop.wait(0.2)

        transport = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        delivery = threading.Thread(target=dispatch, daemon=True)
        transport.start()
        delivery.start()
        if ready is not None:
            ready.set()
        try:
            while not stop.is_set():
                authority.tick(deliver_results=False)
                stop.wait(0.1)
        finally:
            stop.set()
            server.shutdown()
            transport.join(timeout=5)
            delivery.join(timeout=5)
            server.server_close()
            with contextlib.closing(Ledger(directory)) as ledger:
                for stage_id, process in authority.children.items():
                    if ledger.stage(stage_id)["state"] not in OWNING:
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=2)


def client(connection, action, payload):
    endpoint = connection
    authority_directory = connection.get("authority_directory")
    if authority_directory is not None:
        if not isinstance(authority_directory, str) or not authority_directory:
            raise ResourceError("resource authority directory is invalid")
        endpoint = core.load_object(
            local_directory(authority_directory) / "endpoint.json"
        )
        if endpoint.get("authority") != connection["authority"]:
            raise ResourceError("authority mismatch")
    url = endpoint["url"]
    if (
        not url.startswith("http://127.0.0.1:")
        or "/" in url[len("http://127.0.0.1:") :]
    ):
        raise ResourceError(
            "resource transport supports native loopback endpoints only"
        )
    identity = worker_lease.identity_state(endpoint.get("identity"))
    if identity.get("state") != "alive" or not identity.get("identity_verified"):
        raise ResourceError(
            "resource authority process identity is unavailable or stale"
        )
    request = urllib.request.Request(
        url + "/" + action,
        data=canonical(payload).encode(),
        headers={
            "X-Project": connection["project"],
            "Authorization": "Bearer " + connection["token"],
            "Content-Type": "application/json",
        },
    )
    try:

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                raise ResourceError("resource transport does not follow redirects")

        # Do not send local project credentials through environment-configured proxies.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
            if (
                result.get("authority", connection["authority"])
                != connection["authority"]
            ):
                raise ResourceError("authority mismatch")
            return result
    except urllib.error.HTTPError as error:
        raise ResourceError(
            json.loads(error.read()).get("error", "authority error")
        ) from error
    except urllib.error.URLError as error:
        raise ResourceError(
            "resource authority unavailable; ownership has not been cleared"
        ) from error
    except OSError as error:
        # Windows can surface an aborted loopback socket directly instead of
        # wrapping it in URLError. Keep transport failures inside the public API.
        raise ResourceError(
            "resource authority unavailable; ownership has not been cleared"
        ) from error


def connect(directory, project, root):
    authority = Authority(directory)
    registered = authority.config["projects"].get(project)
    if not registered or Path(registered["root"]) != Path(root).resolve():
        raise ResourceError("connection must target the registered project root")
    endpoint = core.load_object(authority.directory / "endpoint.json")
    connection = {
        **endpoint,
        "project": project,
        "token": registered["token"],
        "authority_directory": str(authority.directory),
    }
    path = Path(root) / core.DEFAULT_STATE_DIR / "resources.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _private_atomic_json(path, connection)
    return {"connection": str(path), "authority": connection["authority"]}


def submit_recipe(root, *, recipe, request_id, subscriber=None, lineage=None):
    connection = core.load_object(
        Path(root) / core.DEFAULT_STATE_DIR / "resources.json"
    )
    contract = client(connection, "recipe", {"recipe": recipe})
    return client(
        connection,
        "submit",
        {
            "id": request_id,
            "recipe": recipe,
            "recipe_digest": contract["recipe_digest"],
            "inputs": input_manifest(root, contract["inputs"]),
            "subscriber": subscriber,
            "lineage": lineage,
        },
    )


def prepare_input_contract(root, *, recipe, request_id, lineage=None):
    """Capture expected input hashes for an explicitly delayed submission."""

    connection = core.load_object(
        Path(root) / core.DEFAULT_STATE_DIR / "resources.json"
    )
    registered = client(connection, "recipe", {"recipe": recipe})
    return build_input_contract(
        recipe=recipe,
        recipe_digest=registered["recipe_digest"],
        request_id=request_id,
        lineage=lineage,
        inputs=input_manifest(root, registered["inputs"]),
    )


def submit_input_contract(root, *, input_contract, subscriber=None):
    """Submit exact retained hashes while preserving authority-side verification."""

    retained = validate_input_contract(input_contract)
    connection = core.load_object(
        Path(root) / core.DEFAULT_STATE_DIR / "resources.json"
    )
    registered = client(connection, "recipe", {"recipe": retained["recipe"]})
    if registered["recipe_digest"] != retained["recipe_digest"]:
        raise ResourceError("recipe changed since the input contract was prepared")
    return client(
        connection,
        "submit",
        {
            "id": retained["request_id"],
            "recipe": retained["recipe"],
            "recipe_digest": retained["recipe_digest"],
            "inputs": retained["inputs"],
            "subscriber": subscriber,
            "lineage": retained["lineage"],
        },
    )
