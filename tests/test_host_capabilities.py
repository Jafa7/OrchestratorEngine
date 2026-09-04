from __future__ import annotations

import unittest

from orchestrator_engine import core, host_capabilities


class HostCapabilityTests(unittest.TestCase):
    def test_report_is_versioned_and_has_a_bounded_stable_host_collection(self) -> None:
        report = host_capabilities.all_hosts()
        self.assertEqual(report["schema_version"], core.SCHEMA_VERSION)
        self.assertEqual(report["kind"], host_capabilities.KIND)
        self.assertEqual(report["host_count"], 3)
        self.assertEqual([item["host"] for item in report["hosts"]], [
            "claude",
            "codex",
            "vscode",
        ])
        for capability in report["hosts"]:
            self.assertIn(capability["delivery_mode"], host_capabilities.DELIVERY_MODES)
            self.assertIn(
                capability["live_refresh_support"],
                host_capabilities.LIVE_REFRESH_SUPPORT,
            )

    def test_codex_capability_prefers_live_queue_and_declares_fallback(self) -> None:
        receipt = host_capabilities.receipt_fields("codex")
        self.assertEqual(receipt["delivery_mode"], "session_queue")
        self.assertEqual(receipt["live_refresh_support"], "supported")
        self.assertEqual(receipt["requirement"], "codex queue")
        self.assertEqual(
            receipt["fallback_delivery_mode"], "headless_app_server_turn"
        )

        fallback = host_capabilities.receipt_fields(
            "codex", delivery_mode="headless_app_server_turn"
        )
        self.assertEqual(fallback["delivery_mode"], "headless_app_server_turn")
        self.assertEqual(fallback["live_refresh_support"], "unsupported")
