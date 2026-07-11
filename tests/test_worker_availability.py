import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import worker_diagnostics, workers


class WorkerAvailabilityTests(unittest.TestCase):
    def config(self, root: Path, probe: str) -> dict:
        path = workers.workers_config_path(root)
        path.parent.mkdir(parents=True)
        path.write_text(
            "[workers.w]\n"
            "enabled = true\ncommand = [\"true\"]\nprompt_via = \"stdin\"\n"
            f"availability_probe = [{json.dumps(sys.executable)}, \"-c\", "
            f"{json.dumps(probe)}]\n"
            "availability_timeout_seconds = 0.1\n",
            encoding="utf-8",
        )
        return workers.require_worker(root, "w")

    def test_probe_states_and_private_output_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root, "print('x' * 10000); raise SystemExit(1)")
            result = worker_diagnostics.run_availability_probe(config)
            self.assertEqual(result["status"], "unavailable")
            self.assertGreater(result["output_bytes"], 10_000)
            self.assertEqual(len(result["output_sha256"]), 64)
            self.assertNotIn("output", result)

    def test_timeout_is_probe_error_and_missing_probe_is_configured_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root, "import time; time.sleep(1)")
            self.assertEqual(
                worker_diagnostics.run_availability_probe(config)["status"],
                "probe_error",
            )
            self.assertEqual(
                worker_diagnostics.run_availability_probe({})["status"],
                "not_configured",
            )

    def test_rate_limit_classifier_is_narrow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout, stderr = root / "out", root / "err"
            stdout.write_text("You've hit your session limit", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            self.assertTrue(worker_diagnostics.classify_rate_limit(stdout, stderr))
            stdout.write_text("ordinary command failure", encoding="utf-8")
            self.assertFalse(worker_diagnostics.classify_rate_limit(stdout, stderr))
            stdout.write_text("rate limit is configured", encoding="utf-8")
            self.assertFalse(worker_diagnostics.classify_rate_limit(stdout, stderr))

    def test_probe_timeout_rejects_non_finite_values(self):
        with self.assertRaises(workers.WorkerError):
            workers.validate_worker_config(
                "w",
                {
                    "command": ["true"],
                    "availability_probe": ["true"],
                    "availability_timeout_seconds": math.inf,
                },
            )

    def test_preflight_blocks_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.config(root, "raise SystemExit(1)")
            prompt = root / "prompt"
            prompt.write_text("work", encoding="utf-8")
            with self.assertRaises(workers.WorkerError):
                workers.run_worker(root, worker="w", task_id="T", prompt_file=prompt,
                                   preflight_availability=True)
            self.assertFalse((workers.tasks_root(root) / "T").exists())


if __name__ == "__main__":
    unittest.main()
