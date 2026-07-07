from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from orchestrator_engine import core, workers

WORKERS_TOML = """
[workers.echo]
enabled = true
command = ["{python}", "-c", "import sys; print('worker ran'); sys.exit(0)"]
prompt_via = "stdin"
effort = "high"

[workers.failing]
enabled = true
command = ["{python}", "-c", "import sys; sys.exit(3)"]
prompt_via = "stdin"

[workers.disabled]
enabled = false
command = ["true"]

[workers.sleeper]
enabled = true
command = ["{python}", "-c", "import sys,time; sys.stdin.read(); time.sleep(0.7)"]
prompt_via = "stdin"

[workers.stuck]
enabled = true
command = ["{python}", "-c", "import time; time.sleep(30)"]
prompt_via = "stdin"
timeout_seconds = 1
""".replace("{python}", sys.executable)


class FakePopen:
    command: ClassVar[list[str]] = []
    kwargs: ClassVar[dict[str, object]] = {}

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.__class__.command = command
        self.__class__.kwargs = kwargs
        self.pid = 5150


def write_config(root: Path) -> None:
    path = workers.workers_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKERS_TOML, encoding="utf-8")


def write_prompt(root: Path) -> Path:
    prompt = root / "prompt.md"
    prompt.write_text("do the task", encoding="utf-8")
    return prompt


class WorkerRegistryTests(unittest.TestCase):
    def test_list_workers_reports_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            listing = workers.list_workers(root)
        self.assertIn("echo", listing["workers"])
        self.assertTrue(listing["workers"]["echo"]["enabled"])
        self.assertFalse(listing["workers"]["disabled"]["enabled"])
        self.assertEqual(listing["workers"]["echo"]["effort"], "high")

    def test_missing_config_yields_empty_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            listing = workers.list_workers(root)
        self.assertEqual(listing["workers"], {})

    def test_require_worker_rejects_disabled_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            with self.assertRaises(workers.WorkerError):
                workers.require_worker(root, "disabled")
            with self.assertRaises(workers.WorkerError):
                workers.require_worker(root, "missing")

    def test_invalid_task_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(workers.WorkerError):
                workers.task_dir_for(root, "../escape")


class WorkerRunTests(unittest.TestCase):
    def test_run_worker_spawns_detached_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            descriptor = workers.run_worker(
                root,
                worker="echo",
                task_id="T-1",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            stored = core.load_object(Path(descriptor["descriptor_path"]))
        self.assertEqual(descriptor["status"], "running")
        self.assertEqual(stored["supervisor_pid"], 5150)
        self.assertIn("supervise", FakePopen.command)
        self.assertTrue(FakePopen.kwargs["start_new_session"])

    def test_run_worker_refuses_duplicate_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            workers.run_worker(
                root,
                worker="echo",
                task_id="T-1",
                prompt_file=prompt,
                popen_factory=FakePopen,
            )
            with self.assertRaises(workers.WorkerError):
                workers.run_worker(
                    root,
                    worker="echo",
                    task_id="T-1",
                    prompt_file=prompt,
                    popen_factory=FakePopen,
                )


class WorkerSuperviseTests(unittest.TestCase):
    def test_supervise_success_emits_completed_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="echo",
                task_id="T-OK",
                prompt_file=prompt,
            )
            event = core.load_object(Path(summary["event_path"]))
            signals = core.inbox(root)
            stdout = (
                workers.task_dir_for(root, "T-OK") / "worker-stdout.log"
            ).read_text(encoding="utf-8")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(event["terminal_status"], "completed")
        self.assertEqual(event["task_id"], "T-OK")
        self.assertEqual(len(signals), 1)
        self.assertIn("worker ran", stdout)

    def test_supervise_failure_emits_failed_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="failing",
                task_id="T-FAIL",
                prompt_file=prompt,
            )
            result = core.load_object(
                workers.task_dir_for(root, "T-FAIL") / "result.json"
            )
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("exited with code 3", result["failure_reason"])

    def test_supervise_records_prompt_hash_in_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            workers.supervise_worker(
                root,
                worker="echo",
                task_id="T-EV",
                prompt_file=prompt,
            )
            evidence = core.load_object(
                workers.task_dir_for(root, "T-EV") / "evidence.json"
            )
            expected_hash = core.sha256_file(prompt)
        self.assertEqual(evidence["prompt_sha256"], expected_hash)
        self.assertEqual(evidence["worker_config"]["effort"], "high")

    def test_supervise_touches_descriptor_while_worker_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="sleeper",
                task_id="T-LONG",
                prompt_file=prompt,
                heartbeat_interval_seconds=0.2,
            )
            descriptor = core.load_object(
                workers.task_dir_for(root, "T-LONG") / "task.json"
            )
        self.assertEqual(summary["status"], "completed")
        self.assertIn("last_alive_at", descriptor)
        self.assertEqual(descriptor["status"], "completed")

    def test_supervise_kills_worker_after_configured_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_config(root)
            prompt = write_prompt(root)
            summary = workers.supervise_worker(
                root,
                worker="stuck",
                task_id="T-STUCK",
                prompt_file=prompt,
                heartbeat_interval_seconds=0.2,
            )
            result = core.load_object(
                workers.task_dir_for(root, "T-STUCK") / "result.json"
            )
        self.assertEqual(summary["status"], "timed_out")
        self.assertEqual(result["terminal_status"], "timed_out")
        self.assertIn("exceeded", result["failure_reason"])

    def test_invalid_toml_raises_worker_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = workers.workers_config_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not [valid toml", encoding="utf-8")
            with self.assertRaises(workers.WorkerError):
                workers.load_registry(root)


if __name__ == "__main__":
    unittest.main()
