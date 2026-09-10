"""Native resource scope supervisor; only admitted intents execute user code."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import core, platform_runtime, worker_lease, workers
from .resource_queue import Ledger, ResourceError


def phase_capability(stage, *, maintenance):
    field = "maintenance_token" if maintenance else "launch_token"
    token = stage.get(field)
    if not isinstance(token, str) or not token:
        phase = "maintenance" if maintenance else "work"
        raise ResourceError(
            f"{phase} capability is unavailable; drain before upgrade or recover "
            "the legacy stage explicitly"
        )
    return token


def run_command(command, workspace, output, *, ledger, stage, index, cleanup=False):
    argv = [
        arg.replace("{python}", sys.executable).replace("{workspace}", str(workspace))
        for arg in command["argv"]
    ]
    cwd = (workspace / command.get("cwd", ".")).resolve()
    if not cwd.is_relative_to(workspace.resolve()):
        raise ResourceError("command cwd escaped captured workspace")
    environment = dict(os.environ)
    registered = core.load_object(ledger.directory / "config.json")["projects"][
        stage["project"]
    ]
    environment["ORCHESTRATOR_RESOURCE_CONNECTION"] = str(
        Path(registered["root"]) / core.DEFAULT_STATE_DIR / "resources.json"
    )
    environment["ORCHESTRATOR_RESOURCE_CONTEXT"] = json.dumps(
        {
            "authority": ledger.get("authority"),
            "stage": stage["id"],
            "request": stage["request"],
            "epoch": stage["epoch"],
            "allocation": stage["allocation"],
            "purpose": "maintenance" if cleanup else "work",
            "token": phase_capability(stage, maintenance=cleanup),
        }
    )
    start = time.monotonic()
    process = None
    reason = "completed"
    with output.open("ab") as log:
        try:
            process = platform_runtime.spawn(
                subprocess.Popen,
                argv,
                owned=True,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        except FileNotFoundError as error:
            return {
                "index": index,
                "argv": argv,
                "exit_code": None,
                "reason": "not_started",
                "error": str(error),
                "duration_seconds": time.monotonic() - start,
                "process_quiescent": True,
                "log": str(output),
            }
        group = platform_runtime.process_group(process.pid)
        identity = worker_lease.process_identity(process.pid)
        try:
            if group is None or identity is None:
                raise ResourceError("command containment identity unavailable")
            ledger.annotate(
                stage["id"],
                stage["epoch"],
                command_identity=identity,
                command_group=group,
                command_index=index,
            )
            timeout = command.get("timeout_seconds")
            while True:
                exited = (
                    process.poll() is not None
                    if os.name == "nt"
                    else workers.wait_for_exit(
                        process.pid, timeout_seconds=0, poll_seconds=0.05
                    )
                )
                if exited:
                    break
                current = ledger.stage(stage["id"])
                if current["state"] != "running" or (
                    current.get("cancel_requested") and not cleanup
                ):
                    reason = "cancelled"
                    break
                if timeout is not None and time.monotonic() - start >= timeout:
                    reason = "timed_out"
                    break
                time.sleep(0.1)
        finally:
            # Preserve POSIX PID ownership until the final descendant sweep.
            stopped = workers.terminate_worker(
                process,
                process_group=group,
                reason=reason,
                grace_seconds=0 if reason == "completed" else 1,
                timeout_seconds=5,
                poll_seconds=0.05,
            )
        quiescent = bool(stopped["exited"])
        if os.name != "nt":
            deadline = time.monotonic() + 1
            while (
                worker_lease.process_group_state(group) == "alive"
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            quiescent = quiescent and worker_lease.process_group_state(group) == "gone"
        result = {
            "index": index,
            "argv": argv,
            "exit_code": process.returncode,
            "reason": reason,
            "duration_seconds": time.monotonic() - start,
            "process_quiescent": quiescent,
            "termination": stopped,
            "log": str(output),
        }
        return result


def execute(directory, stage_id, token):
    with contextlib.closing(Ledger(directory)) as ledger:
        stage = ledger.stage(stage_id)
        epoch = stage["epoch"]
        if not token:
            ledger.abort_unadmitted(
                stage_id, epoch, identity=worker_lease.process_identity(os.getpid())
            )
            return
        try:
            stage = ledger.admit(stage_id, token)
        except ResourceError:
            ledger.abort_unadmitted(stage_id, epoch, token=token)
            return
        plan = ledger.request(stage["request"])["plan"]
        workspace = Path(plan["workspace"])
        run_dir = Path(directory) / "runs" / stage_id
        run_dir.mkdir(parents=True, exist_ok=True)
        results = []
        outcome = "passed"
        quiescent = True
        started = False
        try:
            # Captured source inputs are pinned at every stage boundary. Generated
            # outputs must use paths not declared as immutable source inputs.
            for relative, expected in plan["input_manifest"].items():
                path = workspace / relative
                if (
                    path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != expected
                ):
                    outcome = "invalidated"
                    raise ResourceError("captured source inputs changed before launch")
            commands = [(command, False) for command in stage["commands"]]
            commands += [(command, True) for command in stage.get("cleanup", [])]
            for index, (command, cleanup) in enumerate(commands):
                if outcome != "passed" and not cleanup:
                    continue
                if ledger.stage(stage_id).get("cancel_requested") and not cleanup:
                    outcome = "cancelled"
                    continue
                started = True
                result = run_command(
                    command,
                    workspace,
                    run_dir / f"command-{index}.log",
                    ledger=ledger,
                    stage=stage,
                    index=index,
                    cleanup=cleanup,
                )
                results.append(result)
                ledger.annotate(stage_id, epoch, results=results)
                quiescent = quiescent and result["process_quiescent"]
                if result["reason"] == "cancelled":
                    outcome = "cancelled"
                elif result["exit_code"] != 0 or result["reason"] != "completed":
                    outcome = "failed"
                if not quiescent:
                    break
        except (OSError, ResourceError, ValueError) as error:
            if outcome == "passed":
                outcome = "failed"
            # An exception after spawn can hide descendants; require recovery.
            quiescent = not started
            results.append({"error": str(error)})
        registry = ledger.get("registry")
        released = []
        probes = {}
        if quiescent:
            for leaf in stage["allocation"]:
                resource = registry[leaf]
                if not started or resource["release"] == "process":
                    released.append(leaf)
                    continue
                try:
                    probe = run_command(
                        resource["probe"],
                        workspace,
                        run_dir / f"probe-{len(probes)}.log",
                        ledger=ledger,
                        stage=stage,
                        index=-1,
                        cleanup=True,
                    )
                    probes[leaf] = probe
                    if (
                        probe["exit_code"] == 0
                        and probe["process_quiescent"]
                        and probe["reason"] == "completed"
                    ):
                        released.append(leaf)
                except (OSError, ResourceError) as error:
                    probes[leaf] = {"error": str(error)}
        # Coupled resources release together, even when one individual probe passed.
        unsafe_groups = {
            registry[leaf].get("recovery_group")
            for leaf in stage["allocation"]
            if leaf not in released and registry[leaf].get("recovery_group")
        }
        released = [
            leaf
            for leaf in released
            if registry[leaf].get("recovery_group") not in unsafe_groups
        ]
        evidence = {
            "results": results,
            "probes": probes,
            "process_quiescent": quiescent,
            "input_manifest": plan["input_manifest"],
            "recipe_digest": plan["recipe_digest"],
            "resource_incarnations": stage["resource_incarnations"],
        }
        evidence_path = run_dir / "evidence.json"
        core.atomic_json(evidence_path, evidence)
        ledger.finish(
            stage_id,
            epoch,
            outcome,
            released,
            {
                "path": str(evidence_path),
                "sha256": core.sha256_file(evidence_path),
                "process_quiescent": quiescent,
            },
        )
    # Terminal state and outbox insertion are already durable. Project the
    # result before this supervisor exits so delivery does not depend on the
    # long-lived authority surviving an enclosing native process scope.
    try:
        from .resource_service import Authority

        Authority(directory).deliver_pending()
    except (OSError, core.OrchestratorError, ValueError, TypeError, KeyError) as error:
        # The undelivered outbox remains the recovery source of truth.
        print(f"resource terminal delivery deferred: {error}", file=sys.stderr)


def main():
    directory, stage_id = sys.argv[1:]
    token = sys.stdin.buffer.readline(512).decode().strip()
    execute(Path(directory), stage_id, token)


if __name__ == "__main__":
    main()
