"""Explicit resource administration and registered recipe submission."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from . import core


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "resource", help="Coordinate registered local resources"
    )
    commands = parser.add_subparsers(dest="resource_command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--directory", type=Path, required=True)
    init.add_argument("--config", type=Path, required=True)
    update = commands.add_parser("update")
    update.add_argument("--directory", type=Path, required=True)
    update.add_argument("--config", type=Path, required=True)
    update.add_argument("--expected-revision", type=int, required=True)
    server = commands.add_parser("serve")
    server.add_argument("--directory", type=Path, required=True)
    server.add_argument("--port", type=int, default=0)
    attach = commands.add_parser("connect")
    attach.add_argument("--directory", type=Path, required=True)
    attach.add_argument("--project", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--recipe")
    submit.add_argument("--id")
    submit.add_argument("--lineage")
    submit.add_argument("--input-contract", type=Path)
    submit.add_argument("--wake-policy", choices=["never", "always"], default="never")
    submit.add_argument("--target-thread")
    prepare = commands.add_parser("create-input-contract")
    prepare.add_argument("--recipe", required=True)
    prepare.add_argument("--id", required=True)
    prepare.add_argument("--lineage")
    prepare.add_argument("--output", type=Path, required=True)
    for name in ("status", "cancel", "wait"):
        command = commands.add_parser(name)
        command.add_argument("--request", required=name != "status")
        if name == "wait":
            command.add_argument("--timeout-seconds", type=float, default=30)
    commands.add_parser("metrics")
    commands.add_parser("context")
    for name in ("subscribe", "unsubscribe"):
        command = commands.add_parser(name)
        command.add_argument("--request", required=True)
        command.add_argument("--contract-digest", required=True)
        command.add_argument("--subscriber", type=Path, required=True)
    recover = commands.add_parser("recover")
    recover.add_argument("--directory", type=Path, required=True)
    recover.add_argument("--stage", required=True)
    recover.add_argument("--epoch", type=int, required=True)
    recover.add_argument("--evidence", type=Path, required=True)
    recover.add_argument("--release", action="append", required=True)


def run(args, root):
    from .resource_queue import Ledger, ResourceError
    from .resource_service import (
        client,
        connect,
        initialize,
        prepare_input_contract,
        serve,
        submit_input_contract,
        submit_recipe,
        update_configuration,
        validate_input_contract,
    )

    command = args.resource_command
    if command == "init":
        return initialize(args.directory, core.load_object(args.config))
    if command == "update":
        return update_configuration(
            args.directory,
            core.load_object(args.config),
            expected_revision=args.expected_revision,
        )
    if command == "serve":
        serve(args.directory, port=args.port)
        return {"status": "stopped"}
    if command == "connect":
        return connect(args.directory, args.project, root)
    if command == "recover":
        evidence = core.load_object(args.evidence)
        if not evidence.get("quiescent") or not evidence.get("reason"):
            raise ResourceError("recovery requires quiescent evidence and a reason")
        with contextlib.closing(Ledger(args.directory)) as ledger:
            ledger.recover(
                args.stage,
                args.epoch,
                args.release,
                {
                    **evidence,
                    "path": str(args.evidence.resolve()),
                    "sha256": core.sha256_file(args.evidence),
                },
            )
        return {"stage": args.stage, "released": args.release}
    if command == "create-input-contract":
        if args.output.exists():
            raise ResourceError("refusing to replace an existing input contract")
        contract = prepare_input_contract(
            root,
            recipe=args.recipe,
            request_id=args.id,
            lineage=args.lineage,
        )
        core.atomic_json(args.output, contract)
        return {
            "input_contract": str(args.output.resolve()),
            "input_contract_digest": contract["input_contract_digest"],
            "request_id": contract["request_id"],
            "recipe": contract["recipe"],
        }
    if command == "submit":
        if args.wake_policy == "always" and not args.target_thread:
            raise ResourceError("wake delivery requires an explicit target thread")
        if args.input_contract is None:
            if not args.recipe or not args.id:
                raise ResourceError(
                    "resource submit requires --recipe and --id or --input-contract"
                )
        elif args.recipe or args.id or args.lineage:
            raise ResourceError(
                "--input-contract cannot be combined with --recipe, --id or --lineage"
            )
        retained = (
            validate_input_contract(core.load_object(args.input_contract))
            if args.input_contract
            else None
        )
        subscriber_id = retained.get("request_id") if retained else args.id
        subscriber = {
            "id": subscriber_id,
            "wake": args.wake_policy == "always",
        }
        if args.target_thread:
            # Use the existing wake-target contract, never infer another chat's binding.
            from .local_checks import capture_wake_target

            target = capture_wake_target(
                root, state_dir=args.state_dir, wake_policy="always"
            )
            if not target or target.get("target_thread_id") != args.target_thread:
                raise ResourceError(
                    "target thread must match the project's configured binding"
                )
            subscriber["wake_target"] = target
        if retained is not None:
            return submit_input_contract(
                root,
                input_contract=retained,
                subscriber=subscriber,
            )
        return submit_recipe(
            root,
            recipe=args.recipe,
            request_id=args.id,
            subscriber=subscriber,
            lineage=args.lineage,
        )
    connection_path = root / core.DEFAULT_STATE_DIR / "resources.json"
    if command == "context" and os.environ.get("ORCHESTRATOR_RESOURCE_CONNECTION"):
        connection_path = Path(os.environ["ORCHESTRATOR_RESOURCE_CONNECTION"])
    connection = core.load_object(connection_path)
    if command == "context":
        context = json.loads(os.environ.get("ORCHESTRATOR_RESOURCE_CONTEXT", "{}"))
        return client(connection, "context", context)
    payload = {"request": getattr(args, "request", None)}
    if command in {"subscribe", "unsubscribe"}:
        payload.update(
            contract_digest=args.contract_digest,
            subscriber=core.load_object(args.subscriber),
        )
    if command != "wait":
        return client(connection, command, payload)
    if not 0 <= args.timeout_seconds < float("inf"):
        raise ResourceError("wait timeout must be finite and nonnegative")
    deadline = time.monotonic() + args.timeout_seconds
    while True:
        result = client(connection, "status", payload)
        if (
            result["terminal"]
            or result["action_required"]
            or time.monotonic() >= deadline
        ):
            return result
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
