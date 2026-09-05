from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import (
    core,
    github_actions,
    github_pull_requests,
    local_checks,
    platform_runtime,
    workers,
)


class PlatformRuntimeTests(unittest.TestCase):
    def test_capability_report_is_versioned_and_bounded(self) -> None:
        report = platform_runtime.capabilities()

        self.assertEqual(report["schema_version"], core.SCHEMA_VERSION)
        self.assertEqual(report["kind"], platform_runtime.PLATFORM_CAPABILITIES_KIND)
        self.assertEqual(report["portable_core"], "supported")
        self.assertEqual(report["file_locking"], "supported")
        self.assertIn(report["detached_lifecycle"], {"supported", "unsupported"})

    def test_exclusive_file_lock_creates_a_reusable_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "operation.lock"

            with platform_runtime.exclusive_file_lock(path):
                self.assertTrue(path.is_file())
            with platform_runtime.exclusive_file_lock(path):
                self.assertEqual(path.stat().st_size, 1)

    def test_detached_requirement_fails_closed_when_unsupported(self) -> None:
        with (
            mock.patch.object(
                platform_runtime,
                "detached_lifecycle_supported",
                return_value=False,
            ),
            self.assertRaisesRegex(
                platform_runtime.PlatformRuntimeError,
                "requires the Linux detached-runtime capability",
            ),
        ):
            platform_runtime.require_detached_lifecycle("worker run")

    def test_process_alive_does_not_signal_invalid_or_current_pid(self) -> None:
        self.assertFalse(platform_runtime.process_alive(0))
        self.assertTrue(platform_runtime.process_alive(os.getpid()))

    def test_reapers_fail_closed_when_detached_identity_is_unavailable(self) -> None:
        reapers = (
            ("worker reap", workers.reap_worker_tasks),
            ("check reap", local_checks.reap_checks),
            ("ci reap", github_actions.reap_monitors),
            ("pr reap", github_pull_requests.reap_monitors),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with mock.patch.object(
                platform_runtime,
                "detached_lifecycle_supported",
                return_value=False,
            ):
                for feature, reaper in reapers:
                    with (
                        self.subTest(feature=feature),
                        self.assertRaisesRegex(
                            platform_runtime.PlatformRuntimeError,
                            feature,
                        ),
                    ):
                        reaper(root)

            self.assertFalse((root / core.DEFAULT_STATE_DIR).exists())
