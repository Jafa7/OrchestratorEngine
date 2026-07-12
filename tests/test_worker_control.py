from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import time
import unittest
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
