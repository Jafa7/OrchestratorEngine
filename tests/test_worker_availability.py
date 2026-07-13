import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_engine import core, worker_diagnostics, workers


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
            stdout.write_text("You've hit your usage limit", encoding="utf-8")
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

    def test_availability_modes_apply_config_and_cli_precedence(self):
        cases = (
            ("block-unavailable", "available", False),
            ("block-unavailable", "unavailable", True),
            ("block-unavailable", "probe_error", False),
            ("block-unavailable", "not_configured", False),
            ("require-available", "available", False),
            ("require-available", "unavailable", True),
            ("require-available", "probe_error", True),
            ("require-available", "not_configured", True),
        )
        for index, (mode, status, blocked) in enumerate(cases):
            with (
                self.subTest(mode=mode, status=status),
                tempfile.TemporaryDirectory() as directory,
            ):
                    root = Path(directory)
                    config = workers.workers_config_path(root)
                    config.parent.mkdir(parents=True)
                    config.write_text(
                        f'[dispatch]\navailability_mode = "{mode}"\n'
                        '[workers.w]\ncommand = ["true"]\n',
                        encoding="utf-8",
                    )
                    prompt = root / "prompt"
                    prompt.write_text("work", encoding="utf-8")
                    result = {"status": status}
                    with patch(
                        "orchestrator_engine.worker_diagnostics.run_availability_probe",
                        return_value=result,
                    ):
                        if blocked:
                            with self.assertRaises(workers.WorkerError):
                                workers.run_worker(
                                    root,
                                    worker="w",
                                    task_id=f"T-{index}",
                                    prompt_file=prompt,
                                )
                            self.assertFalse(workers.tasks_root(root).exists())
                        else:
                            dispatched = workers.run_worker(
                                root,
                                worker="w",
                                task_id=f"T-{index}",
                                prompt_file=prompt,
                                popen_factory=lambda *args, **kwargs: type(
                                    "Process", (), {"pid": 9000 + index}
                                )(),
                            )
                            self.assertEqual(
                                dispatched["availability_preflight"]["status"], status
                            )
                            self.assertEqual(
                                dispatched["availability_preflight"]["mode"], mode
                            )

    def test_cli_mode_overrides_config_and_legacy_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                '[dispatch]\navailability_mode = "require-available"\n'
                '[workers.w]\ncommand = ["true"]\n',
                encoding="utf-8",
            )
            prompt = root / "prompt"
            prompt.write_text("work", encoding="utf-8")
            dispatched = workers.run_worker(
                root,
                worker="w",
                task_id="T-OFF",
                prompt_file=prompt,
                availability_mode="off",
                popen_factory=lambda *args, **kwargs: type(
                    "Process", (), {"pid": 9100}
                )(),
            )
            self.assertNotIn("availability_preflight", dispatched)
            with self.assertRaisesRegex(workers.WorkerError, "cannot be combined"):
                workers.run_worker(
                    root,
                    worker="w",
                    task_id="T-CONFLICT",
                    prompt_file=prompt,
                    preflight_availability=True,
                    availability_mode="off",
                )

    def test_preflight_metadata_reaches_evidence_without_raw_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                "[workers.w]\n"
                f'command = ["{sys.executable}", "-c", "print(42)"]\n'
                f'availability_probe = ["{sys.executable}", "-c", '
                '"print(\'secret\')"]\n'
                "availability_timeout_seconds = 2\n",
                encoding="utf-8",
            )
            prompt = root / "prompt"
            prompt.write_text("work", encoding="utf-8")
            dispatched = workers.run_worker(
                root,
                worker="w",
                task_id="T-EVIDENCE",
                prompt_file=prompt,
                availability_mode="require-available",
            )
            evidence_path = Path(dispatched["task_dir"]) / "evidence.json"
            for _ in range(100):
                if evidence_path.is_file():
                    break
                time.sleep(0.05)
            evidence = core.load_object(evidence_path)
            snapshot = evidence["availability_preflight"]
            self.assertEqual(snapshot["status"], "available")
            self.assertIn("output_sha256", snapshot)
            self.assertNotIn("output", snapshot)
            self.assertNotIn("secret", json.dumps(snapshot))
            descriptor_path = Path(dispatched["descriptor_path"])
            for _ in range(100):
                descriptor = core.load_object(descriptor_path)
                if descriptor.get("status") in core.TERMINAL_STATUSES:
                    break
                time.sleep(0.05)
            self.assertIn(descriptor["status"], core.TERMINAL_STATUSES)


if __name__ == "__main__":
    unittest.main()
