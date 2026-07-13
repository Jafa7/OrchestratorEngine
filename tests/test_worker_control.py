from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from orchestrator_engine import core, workers


class FakePopen:
    next_pid: ClassVar[int] = 7000

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = self.__class__.next_pid
        self.__class__.next_pid += 1


def write_config(root: Path, *, global_limit: int = 1) -> None:
    path = workers.workers_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
[dispatch]
max_concurrent = {global_limit}

[workers.slow]
command = ["{sys.executable}", "-c", "import time; time.sleep(30)"]
prompt_via = "stdin"
max_concurrent = 1
""",
        encoding="utf-8",
    )


def prompt(root: Path, name: str) -> Path:
    path = root / f"{name}.md"
    path.write_text(f"work: {name}", encoding="utf-8")
    return path


def wait_for(path: Path, predicate, timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            with contextlib.suppress(OSError, core.OrchestratorError):
                value = core.load_object(path)
                if predicate(value):
                    return value
        time.sleep(0.05)
    raise AssertionError(f"condition not reached for {path}")


class WorkerQueueTests(unittest.TestCase):
    def test_global_limit_queues_and_tick_admits_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            first = workers.run_worker(
                root,
                worker="slow",
                task_id="T-1",
                prompt_file=prompt(root, "one"),
                popen_factory=FakePopen,
            )
            second = workers.run_worker(
                root,
                worker="slow",
                task_id="T-2",
                prompt_file=prompt(root, "two"),
                popen_factory=FakePopen,
            )
            third = workers.run_worker(
                root,
                worker="slow",
                task_id="T-3",
                prompt_file=prompt(root, "three"),
                popen_factory=FakePopen,
            )
            first_path = Path(first["descriptor_path"])
            first_stored = core.load_object(first_path)
            first_stored["status"] = "completed"
            core.atomic_json(first_path, first_stored)
            tick = workers.queue_tick(root, popen_factory=FakePopen)
            second_stored = core.load_object(Path(second["descriptor_path"]))
            third_stored = core.load_object(Path(third["descriptor_path"]))

        self.assertEqual(first["status"], "starting")
        self.assertEqual(second["status"], "queued")
        self.assertEqual(third["status"], "queued")
        self.assertEqual(tick["admitted_task_ids"], ["T-2"])
        self.assertEqual(second_stored["status"], "starting")
        self.assertEqual(third_stored["status"], "queued")

    def test_profile_limit_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = workers.workers_config_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                '[workers.bad]\ncommand = ["true"]\nmax_concurrent = 0\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(workers.WorkerError, "max_concurrent"):
                workers.load_registry(root)

    def test_exact_duplicate_requires_recorded_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root, global_limit=2)
            same_prompt = prompt(root, "same")
            workers.run_worker(
                root,
                worker="slow",
                task_id="T-ORIGINAL",
                prompt_file=same_prompt,
                popen_factory=FakePopen,
            )
            with self.assertRaisesRegex(workers.WorkerError, "exact duplicate"):
                workers.run_worker(
                    root,
                    worker="slow",
                    task_id="T-BLOCKED",
                    prompt_file=same_prompt,
                    popen_factory=FakePopen,
                )
            allowed = workers.run_worker(
                root,
                worker="slow",
                task_id="T-ALLOWED",
                prompt_file=same_prompt,
                popen_factory=FakePopen,
                allow_duplicate=True,
                duplicate_reason="independent verification",
            )
            original_path = workers.task_dir_for(root, "T-ORIGINAL") / "task.json"
            original = core.load_object(original_path)
            original["status"] = "completed"
            core.atomic_json(original_path, original)
            workers.release_dispatch_claim(root, original, state_dir=".orchestrator")
            with self.assertRaisesRegex(workers.WorkerError, "exact duplicate"):
                workers.run_worker(
                    root,
                    worker="slow",
                    task_id="T-THIRD",
                    prompt_file=same_prompt,
                    popen_factory=FakePopen,
                )

        self.assertEqual(
            allowed["duplicate_override"]["conflicts_with_task_id"],
            "T-ORIGINAL",
        )
        self.assertEqual(allowed["status"], "queued")

    def test_queue_tick_restores_entry_when_spawn_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            active = workers.run_worker(
                root,
                worker="slow",
                task_id="T-SPAWN-ACTIVE",
                prompt_file=prompt(root, "spawn-active"),
                popen_factory=FakePopen,
            )
            queued = workers.run_worker(
                root,
                worker="slow",
                task_id="T-SPAWN-QUEUED",
                prompt_file=prompt(root, "spawn-queued"),
                popen_factory=FakePopen,
            )
            active_descriptor = core.load_object(Path(active["descriptor_path"]))
            active_descriptor["status"] = "completed"
            core.atomic_json(Path(active["descriptor_path"]), active_descriptor)

            def fail_spawn(command: list[str], **kwargs: Any) -> Any:
                raise OSError("spawn unavailable")

            tick = workers.queue_tick(root, popen_factory=fail_spawn)
            stored = core.load_object(Path(queued["descriptor_path"]))
            pending = workers.pending_queue_path(
                root, "T-SPAWN-QUEUED", state_dir=".orchestrator"
            )
            pending_exists = pending.is_file()

        self.assertEqual(tick["admitted_count"], 0)
        self.assertEqual(stored["status"], "queued")
        self.assertTrue(pending_exists)

    def test_task_intent_is_snapshotted_into_effective_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            intent = root / "intent.json"
            intent.write_text(
                json.dumps(
                    {
                        "role": "review",
                        "risk": "high",
                        "verification": "full",
                        "permissions": "readonly",
                        "authorizations": {"network": False},
                    }
                ),
                encoding="utf-8",
            )
            dispatched = workers.run_worker(
                root,
                worker="slow",
                task_id="T-INTENT",
                prompt_file=prompt(root, "intent-task"),
                intent_file=intent,
                popen_factory=FakePopen,
            )
            effective = Path(dispatched["effective_prompt_file"]).read_text(
                encoding="utf-8"
            )

        self.assertEqual(dispatched["task_intent"]["role"], "review")
        self.assertIn("ORCHESTRATOR_TASK_INTENT v1", effective)
        self.assertIn('"permissions": "readonly"', effective)

    def test_opt_in_intent_gate_rejects_excess_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                """
[dispatch]
enforce_intent = true
[workers.full]
command = ["true"]
permission_profile = "full"
""",
                encoding="utf-8",
            )
            intent = root / "intent.json"
            intent.write_text(json.dumps({"permissions": "readonly"}), encoding="utf-8")
            with self.assertRaisesRegex(workers.WorkerError, "exceeds task intent"):
                workers.run_worker(
                    root,
                    worker="full",
                    task_id="T-GATE",
                    prompt_file=prompt(root, "gate"),
                    intent_file=intent,
                    popen_factory=FakePopen,
                )

    def test_strict_intent_accepts_complete_admission_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                """
[dispatch]
intent_enforcement = "strict"
[workers.reviewer]
command = ["true"]
permission_profile = "readonly"
[workers.reviewer.admission]
roles = ["review"]
max_risk = "high"
verification = ["focused", "full"]
authorizations = { commit = false, push = false, network = false }
""",
                encoding="utf-8",
            )
            intent = root / "intent.json"
            intent.write_text(
                json.dumps(
                    {
                        "role": "review",
                        "risk": "medium",
                        "verification": "focused",
                        "permissions": "readonly",
                        "authorizations": {},
                    }
                ),
                encoding="utf-8",
            )
            dispatched = workers.run_worker(
                root,
                worker="reviewer",
                task_id="T-STRICT-OK",
                prompt_file=prompt(root, "strict-ok"),
                intent_file=intent,
                popen_factory=FakePopen,
            )
        self.assertEqual(dispatched["status"], "starting")
        self.assertEqual(dispatched["intent_admission"]["mode"], "strict")
        self.assertEqual(
            dispatched["intent_admission"]["worker_admission"]["roles"],
            ["review"],
        )

    def test_strict_permissions_only_does_not_require_admission_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                '[dispatch]\nintent_enforcement = "strict"\n'
                '[workers.w]\ncommand = ["true"]\n'
                'permission_profile = "readonly"\n',
                encoding="utf-8",
            )
            intent = root / "intent.json"
            intent.write_text(
                json.dumps({"permissions": "readonly"}), encoding="utf-8"
            )
            dispatched = workers.run_worker(
                root,
                worker="w",
                task_id="T-PERMISSION-ONLY",
                prompt_file=prompt(root, "permission-only"),
                intent_file=intent,
                popen_factory=FakePopen,
            )
        self.assertEqual(dispatched["intent_admission"]["worker_admission"], {})

    def test_strict_intent_rejects_mismatch_and_missing_declarations(self) -> None:
        base_admission = {
            "roles": ["review"],
            "max_risk": "medium",
            "verification": ["focused"],
            "authorizations": {"commit": False, "push": False, "network": False},
        }
        cases = (
            ({"role": "implementation"}, "roles do not include"),
            ({"risk": "high"}, "exceeds worker max_risk"),
            ({"verification": "full"}, "does not include"),
            ({"permissions": "readonly"}, "permission_profile"),
            ({"authorizations": {"commit": False}}, "authorization commit"),
            ({"authorizations": {"push": False}}, "authorization push"),
            ({"authorizations": {"network": False}}, "authorization network"),
        )
        for index, (intent_value, message) in enumerate(cases):
            with self.subTest(intent=intent_value):
                admission = json.loads(json.dumps(base_admission))
                permission = "full" if "permissions" in intent_value else "readonly"
                if "authorizations" in intent_value:
                    key = next(iter(intent_value["authorizations"]))
                    admission["authorizations"][key] = True
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary).resolve()
                    config = workers.workers_config_path(root)
                    config.parent.mkdir(parents=True)
                    roles = ", ".join(f'"{item}"' for item in admission["roles"])
                    verification = ", ".join(
                        f'"{item}"' for item in admission["verification"]
                    )
                    authorizations = ", ".join(
                        f"{key} = {str(value).lower()}"
                        for key, value in admission["authorizations"].items()
                    )
                    config.write_text(
                        "[dispatch]\nintent_enforcement = \"strict\"\n"
                        "[workers.w]\ncommand = [\"true\"]\n"
                        f'permission_profile = "{permission}"\n'
                        "[workers.w.admission]\n"
                        f"roles = [{roles}]\n"
                        f'max_risk = "{admission["max_risk"]}"\n'
                        f"verification = [{verification}]\n"
                        f"authorizations = {{ {authorizations} }}\n",
                        encoding="utf-8",
                    )
                    intent = root / "intent.json"
                    intent.write_text(json.dumps(intent_value), encoding="utf-8")
                    with self.assertRaisesRegex(workers.WorkerError, message):
                        workers.run_worker(
                            root,
                            worker="w",
                            task_id=f"T-STRICT-{index}",
                            prompt_file=prompt(root, f"strict-{index}"),
                            intent_file=intent,
                            popen_factory=FakePopen,
                        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                '[dispatch]\nintent_enforcement = "strict"\n'
                '[workers.w]\ncommand = ["true"]\npermission_profile = "readonly"\n'
                '[workers.w.admission]\nmax_risk = "high"\n',
                encoding="utf-8",
            )
            intent = root / "intent.json"
            intent.write_text(json.dumps({"role": "review"}), encoding="utf-8")
            with self.assertRaisesRegex(workers.WorkerError, "requires.*roles"):
                workers.run_worker(
                    root,
                    worker="w",
                    task_id="T-MISSING",
                    prompt_file=prompt(root, "missing"),
                    intent_file=intent,
                    popen_factory=FakePopen,
                )

    def test_dispatch_rejects_ambiguous_intent_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                '[dispatch]\nenforce_intent = true\nintent_enforcement = "strict"\n'
                '[workers.w]\ncommand = ["true"]\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(workers.WorkerError, "cannot specify both"):
                workers.load_dispatch_config(root)

    def test_retry_records_bounded_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            original = workers.run_worker(
                root,
                worker="slow",
                task_id="T-RETRY",
                prompt_file=prompt(root, "retry"),
                popen_factory=FakePopen,
            )
            descriptor_path = Path(original["descriptor_path"])
            descriptor = core.load_object(descriptor_path)
            descriptor["status"] = "rate_limited"
            core.atomic_json(descriptor_path, descriptor)
            retried = workers.retry_worker_task(
                root,
                task_id="T-RETRY",
                reason="provider quota reset",
                max_attempts=2,
                delay_seconds=60,
            )

        self.assertEqual(retried["task_id"], "T-RETRY-a2")
        self.assertEqual(retried["retry_lineage"]["attempt"], 2)
        self.assertEqual(retried["retry_lineage"]["max_attempts"], 2)
        self.assertEqual(retried["status"], "queued")


class WorkerWaitTests(unittest.TestCase):
    def test_wait_transitions_without_reading_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-WAIT")
            task_dir.mkdir(parents=True)
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-WAIT",
                    "worker": "slow",
                    "status": "running",
                    "progress": {
                        "heartbeat_count": 2,
                        "stdout_bytes": 1_000_000,
                    },
                },
            )
            (task_dir / "worker-stdout.log").write_text(
                "private output must not be read", encoding="utf-8"
            )
            clock = [0.0]

            def finish_worker(seconds: float) -> None:
                clock[0] += seconds
                core.atomic_json(
                    task_dir / "result.json",
                    {
                        "schema_version": 1,
                        "kind": "WORKER_RESULT",
                        "task_id": "T-WAIT",
                        "worker": "slow",
                        "terminal_status": "completed",
                        "exit_code": 0,
                        "failure_reason": None,
                        "duration_seconds": 10.0,
                        "finished_at": core.utc_now(),
                    },
                )
                descriptor = core.load_object(task_dir / "task.json")
                descriptor["status"] = "completed"
                core.atomic_json(task_dir / "task.json", descriptor)

            result = workers.wait_for_worker_task(
                root,
                task_id="T-WAIT",
                interval_seconds=0.1,
                monotonic=lambda: clock[0],
                sleeper=finish_worker,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress"]["stdout_bytes"], 1_000_000)
        self.assertNotIn("stdout", result)

    def test_wait_does_not_finish_before_descriptor_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-FINALIZING")
            task_dir.mkdir(parents=True)
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-FINALIZING",
                    "worker": "slow",
                    "status": "running",
                    "supervisor_pid": os.getpid(),
                },
            )
            core.atomic_json(
                task_dir / "result.json",
                {
                    "schema_version": 1,
                    "kind": "WORKER_RESULT",
                    "task_id": "T-FINALIZING",
                    "worker": "slow",
                    "terminal_status": "completed",
                    "exit_code": 0,
                    "failure_reason": None,
                    "duration_seconds": 1.0,
                    "finished_at": core.utc_now(),
                },
            )
            snapshot = workers.worker_wait_snapshot(
                root, task_id="T-FINALIZING"
            )

        self.assertEqual(snapshot["status"], "finalizing")
        self.assertEqual(snapshot["terminal_status"], "completed")
        self.assertFalse(snapshot["terminal"])

    def test_wait_timeout_preserves_running_task_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-LONG")
            task_dir.mkdir(parents=True)
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-LONG",
                    "worker": "slow",
                    "status": "running",
                },
            )
            clock = [0.0]
            result = workers.wait_for_worker_task(
                root,
                task_id="T-LONG",
                interval_seconds=0.1,
                timeout_seconds=0.2,
                monotonic=lambda: clock[0],
                sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            )

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["wait_status"], "timed_out")
        self.assertFalse(result["terminal"])

    def test_wait_snapshot_reports_stale_live_supervisor(self) -> None:
        now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-STALE")
            task_dir.mkdir(parents=True)
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-STALE",
                    "worker": "slow",
                    "status": "running",
                    "supervisor_pid": os.getpid(),
                    "last_alive_at": (now - timedelta(seconds=120)).isoformat(),
                },
            )
            snapshot = workers.worker_wait_snapshot(
                root,
                task_id="T-STALE",
                stale_after_seconds=90,
                now=now,
            )

        self.assertEqual(snapshot["health"]["status"], "heartbeat_stale")
        self.assertEqual(snapshot["health"]["supervisor_state"], "alive")

    def test_wait_requires_action_for_terminal_descriptor_without_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-TORN")
            task_dir.mkdir(parents=True)
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-TORN",
                    "worker": "slow",
                    "status": "completed",
                },
            )
            result = workers.wait_for_worker_task(root, task_id="T-TORN")

        self.assertEqual(result["wait_status"], "action_required")
        self.assertEqual(
            result["health"]["status"], "terminal_result_unreadable"
        )


class WorkerCancelTests(unittest.TestCase):
    def test_cancel_queued_task_emits_terminal_artifacts_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            workers.run_worker(
                root,
                worker="slow",
                task_id="T-ACTIVE",
                prompt_file=prompt(root, "active"),
                popen_factory=FakePopen,
            )
            queued = workers.run_worker(
                root,
                worker="slow",
                task_id="T-QUEUED",
                prompt_file=prompt(root, "queued"),
                popen_factory=FakePopen,
            )
            first = workers.cancel_worker_task(
                root,
                task_id="T-QUEUED",
                mode="graceful",
                reason="no longer needed",
            )
            second = workers.cancel_worker_task(
                root,
                task_id="T-QUEUED",
                mode="graceful",
                reason="repeat",
            )
            task_dir = Path(queued["task_dir"])
            result = core.load_object(task_dir / "result.json")
            signals = core.inbox(root)

        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(second["status"], "already_terminal")
        self.assertEqual(result["terminal_status"], "cancelled")
        self.assertEqual(len(signals), 1)

    def test_running_forced_cancel_is_applied_by_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            dispatched = workers.run_worker(
                root,
                worker="slow",
                task_id="T-RUNNING",
                prompt_file=prompt(root, "running"),
            )
            descriptor_path = Path(dispatched["descriptor_path"])
            wait_for(descriptor_path, lambda value: value.get("status") == "running")
            requested = workers.cancel_worker_task(
                root,
                task_id="T-RUNNING",
                mode="forced",
                reason="operator stop",
            )
            task_dir = Path(dispatched["task_dir"])
            result = wait_for(
                task_dir / "result.json",
                lambda value: value.get("terminal_status") == "cancelled",
            )
            ack = core.load_object(task_dir / "control" / "cancel.ack.json")
            wait_for(
                descriptor_path,
                lambda value: value.get("status") == "cancelled",
            )
            time.sleep(0.1)

        self.assertEqual(requested["status"], "requested")
        self.assertEqual(result["terminal_status"], "cancelled")
        self.assertEqual(result["termination"]["signals"][0]["signal"], "SIGKILL")
        self.assertEqual(ack["status"], "applied")

    def test_running_graceful_cancel_starts_with_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            dispatched = workers.run_worker(
                root,
                worker="slow",
                task_id="T-GRACEFUL",
                prompt_file=prompt(root, "graceful"),
            )
            descriptor_path = Path(dispatched["descriptor_path"])
            wait_for(descriptor_path, lambda value: value.get("status") == "running")
            workers.cancel_worker_task(
                root,
                task_id="T-GRACEFUL",
                mode="graceful",
                reason="operator stop",
            )
            task_dir = Path(dispatched["task_dir"])
            result = wait_for(
                task_dir / "result.json",
                lambda value: value.get("terminal_status") == "cancelled",
            )
            wait_for(descriptor_path, lambda value: value.get("status") == "cancelled")
            time.sleep(0.1)

        self.assertEqual(result["termination"]["signals"][0]["signal"], "SIGTERM")
        self.assertFalse(result["termination"]["escalated"])


if __name__ == "__main__":
    unittest.main()
