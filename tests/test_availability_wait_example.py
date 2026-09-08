from __future__ import annotations

import runpy
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from orchestrator_engine import worker_diagnostics, workers


class AvailabilityWaitExampleTests(unittest.TestCase):
    def setUp(self):
        self.wait = runpy.run_path(
            str(
                Path(__file__).resolve().parents[1]
                / "examples"
                / "wait_for_availability.py"
            )
        )["wait_for_availability"]

    def call(self, **kwargs):
        return self.wait(
            Path("/synthetic"),
            worker="implementation",
            retry_seconds=300,
            maximum_retry_seconds=600,
            **kwargs,
        )

    def test_waits_without_model_retries_and_returns_once_after_recovery(self):
        with (
            mock.patch.object(workers, "require_worker", return_value={}) as config,
            mock.patch.object(
                worker_diagnostics,
                "run_availability_probe",
                side_effect=[
                    {"status": "unavailable"},
                    {"status": "unavailable"},
                    {"status": "unavailable"},
                    {"status": "available"},
                ],
            ) as probe,
            mock.patch("time.sleep") as sleep,
        ):
            result = self.call()
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["probe_count"], 4)
        self.assertEqual(probe.call_count, config.call_count)
        self.assertEqual(
            sleep.call_args_list, [mock.call(300), mock.call(600), mock.call(600)]
        )

    def test_unknown_probe_is_not_success_or_a_retry_loop(self):
        for status in ("not_configured", "probe_error"):
            with (
                self.subTest(status=status),
                mock.patch.object(workers, "require_worker", return_value={}),
                mock.patch.object(
                    worker_diagnostics,
                    "run_availability_probe",
                    return_value={"status": status},
                ),
                mock.patch("time.sleep") as sleep,
            ):
                self.assertEqual(self.call()["status"], status)
                sleep.assert_not_called()

    def test_known_reset_time_defers_the_first_probe(self):
        with (
            mock.patch.object(workers, "require_worker", return_value={}),
            mock.patch.object(
                worker_diagnostics,
                "run_availability_probe",
                return_value={"status": "available"},
            ),
            mock.patch("time.sleep") as sleep,
        ):
            self.call(not_before=datetime.now(UTC) + timedelta(hours=5))
        self.assertGreater(sleep.call_args.args[0], 17990)

    def test_disabled_profile_and_process_interruption_stop_waiting(self):
        with mock.patch.object(
            workers, "require_worker", side_effect=workers.WorkerError("disabled")
        ), self.assertRaises(workers.WorkerError):
            self.call()
        with (
            mock.patch.object(workers, "require_worker", return_value={}),
            mock.patch.object(
                worker_diagnostics,
                "run_availability_probe",
                return_value={"status": "unavailable"},
            ),
            mock.patch("time.sleep", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.call()
