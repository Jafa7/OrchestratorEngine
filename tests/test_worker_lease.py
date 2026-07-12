from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestrator_engine import core, worker_lease, workers


class WorkerLeaseTests(unittest.TestCase):
    def test_reap_recovers_new_task_never_claimed_by_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-UNCLAIMED")
            task_dir.mkdir(parents=True)
            created = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            core.atomic_json(
                task_dir / "task.json",
                {
                    "schema_version": 1,
                    "kind": workers.TASK_KIND,
                    "task_id": "T-UNCLAIMED",
                    "worker": "test",
                    "status": "starting",
                    "prompt_file": str(root / "prompt.md"),
                    "prompt_sha256": "a" * 64,
                    "task_dir": str(task_dir),
                    "created_at": created,
                    "lease_required": True,
                },
            )

            report = workers.reap_worker_tasks(root)
            result = core.load_object(task_dir / "result.json")

        self.assertEqual(report["reaped_count"], 1)
        self.assertEqual(result["failure_class"], "supervisor_lost")

    def test_process_identity_rejects_reused_pid_token(self) -> None:
        observed = worker_lease.process_identity(os.getpid())
        assert observed is not None
        recorded = {**observed, "start_ticks": observed["start_ticks"] - 1}

        state = worker_lease.identity_state(recorded)
        stopped = worker_lease.stop_worker_tree(
            worker_pid=os.getpid(),
            worker_pgid=os.getpid(),
            worker_identity=recorded,
            reason="test",
            grace_seconds=0,
            timeout_seconds=0,
        )

        self.assertEqual(state["state"], "gone")
        self.assertFalse(state["identity_verified"])
        self.assertEqual(stopped["stop_outcome"], "refused_identity_mismatch")
        self.assertEqual(stopped["signals"], [])

    def test_reap_finalizes_dead_supervisor_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-LOST")
            task_dir.mkdir(parents=True)
            created = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            descriptor = {
                "schema_version": 1,
                "kind": workers.TASK_KIND,
                "task_id": "T-LOST",
                "worker": "test",
                "status": "running",
                "prompt_file": str(root / "prompt.md"),
                "prompt_sha256": "a" * 64,
                "effective_prompt_file": str(task_dir / "effective-prompt.md"),
                "effective_prompt_sha256": "b" * 64,
                "task_dir": str(task_dir),
                "created_at": created,
            }
            core.atomic_json(task_dir / "task.json", descriptor)
            lease = {
                "schema_version": 1,
                "kind": worker_lease.LEASE_KIND,
                "task_id": "T-LOST",
                "worker": "test",
                "status": "held",
                "supervisor_pid": 999999999,
                "supervisor_identity": {
                    "source": worker_lease.IDENTITY_SOURCE,
                    "pid": 999999999,
                    "start_ticks": 1,
                },
                "lease_expiry_seconds": 1.0,
                "acquired_at": created,
                "renewed_at": created,
            }
            core.atomic_json(worker_lease.lease_path(task_dir), lease)

            first = workers.reap_worker_tasks(root)
            second = workers.reap_worker_tasks(root)
            result = core.load_object(task_dir / "result.json")
            stored = core.load_object(task_dir / "task.json")
            event = core.load_object(Path(stored["event_path"]))

        self.assertEqual(first["reaped_count"], 1)
        self.assertEqual(second["reaped_count"], 0)
        self.assertEqual(result["failure_class"], "supervisor_lost")
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(event["terminal_status"], "failed")

    def test_terminal_claim_preserves_first_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            task_dir = workers.task_dir_for(root, "T-RACE")
            task_dir.mkdir(parents=True)
            now = core.utc_now()

            def result(status: str) -> dict[str, object]:
                return {
                    "schema_version": 1,
                    "kind": "WORKER_RESULT",
                    "task_id": "T-RACE",
                    "worker": "test",
                    "terminal_status": status,
                    "exit_code": 0 if status == "completed" else None,
                    "failure_reason": None,
                    "duration_seconds": 0.0,
                    "stdout_path": str(task_dir / "worker-stdout.log"),
                    "stderr_path": str(task_dir / "worker-stderr.log"),
                    "started_at": now,
                    "finished_at": now,
                }

            evidence = {
                "schema_version": 1,
                "kind": "WORKER_EVIDENCE",
                "task_id": "T-RACE",
                "worker": "test",
                "command": ["test"],
                "prompt_file": "prompt.md",
                "prompt_sha256": "a" * 64,
                "worker_config": {},
                "started_at": now,
                "finished_at": now,
            }
            first = workers.finalize_terminal_task(
                root,
                task_id="T-RACE",
                task_dir=task_dir,
                result=result("completed"),
                evidence=evidence,
            )
            second = workers.finalize_terminal_task(
                root,
                task_id="T-RACE",
                task_dir=task_dir,
                result=result("failed"),
                evidence=evidence,
            )

        self.assertEqual(first["outcome"], "claimed")
        self.assertEqual(second["outcome"], "lost")
        self.assertEqual(second["result"]["terminal_status"], "completed")


if __name__ == "__main__":
    unittest.main()
