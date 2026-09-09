"""Loopback transport and native authority for cooperative resource recipes."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import core, platform_runtime, worker_lease
from .resource_queue import (
    OWNING,
    Ledger,
    ResourceError,
    canonical,
    digest,
    identifier,
    validate_subscriber,
)


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
    return path.resolve()


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
    with contextlib.closing(Ledger(directory, create=True)) as ledger:
        ledger.configure(config["resources"])
        config = json.loads(canonical(config))
        config["authority"] = ledger.get("authority")
        for project in config["projects"].values():
            project["token"] = secrets.token_urlsafe(32)
            project["root"] = str(Path(project["root"]).resolve())
        core.atomic_json(directory / "config.json", config)
    with contextlib.suppress(OSError):
        (directory / "config.json").chmod(0o600)
    return {"authority": config["authority"], "directory": str(directory)}


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
        self.config = core.load_object(self.directory / "config.json")
        self.lock = threading.RLock()
        self.children = {}
        with contextlib.closing(Ledger(self.directory)) as ledger:
            if ledger.get("authority") != self.config["authority"]:
                raise ResourceError(
                    "authority identity mismatch; reconcile restored state"
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
                if (
                    stage["project"] != project
                    or stage["request"] != request
                    or stage["state"] != "running"
                    or stage.get("cancel_requested")
                    or stage["epoch"] != payload.get("epoch")
                    or not hmac.compare_digest(
                        stage.get("launch_token", ""), payload.get("token", "")
                    )
                ):
                    raise ResourceError("resource execution context is revoked")
                return {
                    "authority": ledger.get("authority"),
                    "valid": True,
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
        subscriber = payload.get("subscriber")
        if subscriber is not None:
            validate_subscriber(subscriber)
        # Compare replay before inspecting live inputs: accepted inputs are immutable.
        contract = {
            "recipe": recipe,
            "inputs": payload["inputs"],
            "lineage": payload.get("lineage"),
        }
        fingerprint = digest(contract)
        with self.lock, contextlib.closing(Ledger(self.directory)) as ledger:
            row = ledger.db.execute(
                "SELECT id FROM requests WHERE project=? AND external=?",
                (project, external),
            ).fetchone()
            if row:
                plan = ledger.request(row[0])["plan"]
                if plan["contract_digest"] != fingerprint:
                    raise ResourceError(
                        "request ID already has different immutable inputs"
                    )
                return {
                    "request": row[0],
                    "contract_digest": digest(plan),
                    "idempotent": True,
                }
        actual = input_manifest(registered["root"], recipe["inputs"])
        if actual != payload["inputs"]:
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
            "lineage": payload.get("lineage"),
        }
        with self.lock, contextlib.closing(Ledger(self.directory)) as ledger:
            # Concurrent replay may have won while this snapshot was captured.
            row = ledger.db.execute(
                "SELECT id FROM requests WHERE project=? AND external=?",
                (project, external),
            ).fetchone()
            if row:
                existing = ledger.request(row[0])["plan"]
                if existing["contract_digest"] != fingerprint:
                    raise ResourceError("concurrent request ID conflict")
                return {
                    "request": row[0],
                    "contract_digest": digest(existing),
                    "idempotent": True,
                }
            request = ledger.submit(project, external, plan, subscriber)
        return {
            "request": request,
            "contract_digest": digest(plan),
            "idempotent": False,
        }

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
                    core.atomic_json(
                        result,
                        {
                            **value,
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
            if deliver_results:
                self.deliver(ledger)
            for stage_id, process in list(self.children.items()):
                if (
                    ledger.stage(stage_id)["state"] not in OWNING
                    and process.poll() is not None
                ):
                    del self.children[stage_id]


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

        previous_endpoint = directory / "endpoint.json"
        if port == 0 and previous_endpoint.exists():
            port = int(core.load_object(previous_endpoint)["url"].rsplit(":", 1)[1])
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True
        server.timeout = 0.1
        endpoint = {
            "url": f"http://127.0.0.1:{server.server_port}",
            "authority": authority.config["authority"],
            "identity": worker_lease.process_identity(os.getpid()),
        }
        core.atomic_json(directory / "endpoint.json", endpoint)
        stop = stop or threading.Event()

        def dispatch():
            while not stop.is_set():
                with contextlib.closing(Ledger(directory)) as ledger:
                    authority.deliver(ledger)
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
    url = connection["url"]
    if (
        not url.startswith("http://127.0.0.1:")
        or "/" in url[len("http://127.0.0.1:") :]
    ):
        raise ResourceError(
            "resource transport supports native loopback endpoints only"
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


def connect(directory, project, root):
    authority = Authority(directory)
    registered = authority.config["projects"].get(project)
    if not registered or Path(registered["root"]) != Path(root).resolve():
        raise ResourceError("connection must target the registered project root")
    endpoint = core.load_object(authority.directory / "endpoint.json")
    connection = {**endpoint, "project": project, "token": registered["token"]}
    path = Path(root) / core.DEFAULT_STATE_DIR / "resources.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    core.atomic_json(path, connection)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
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
