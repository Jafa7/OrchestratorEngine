from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import upgrade, workers


class UpgradeCheckTests(unittest.TestCase):
    def test_upgrade_check_is_bounded_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                "[dispatch]\n"
                'intent_enforcement = "strict"\n'
                "[workers.claude]\n"
                'command = ["claude", "-p"]\n'
                'prompt_via = "stdin"\n'
                "[workers.claude.admission]\n"
                'roles = ["implementation"]\n',
                encoding="utf-8",
            )
            before = config.read_bytes()
            report = upgrade.run_upgrade_check(root)
            after = config.read_bytes()

        self.assertEqual(report["kind"], upgrade.UPGRADE_CHECK_KIND)
        self.assertEqual(report["status"], "review_required")
        self.assertEqual(before, after)
        codes = {item["code"] for item in report["next_actions"]}
        self.assertIn("strict_ai_verification_not_declared", codes)
        self.assertLessEqual(len(report["next_actions"]), upgrade.MAX_NEXT_ACTIONS)
        self.assertEqual(
            {item["code"] for item in report["manual_checks"]},
            {"reusable_prompt_audit", "smoke_dispatch"},
        )

    def test_upgrade_check_reports_invalid_registry_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text("not [valid toml", encoding="utf-8")
            report = upgrade.run_upgrade_check(root)

        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["dispatch"]["readable"])
        self.assertEqual(report["workers"]["diagnostics"][0]["worker"], "<registry>")

    def test_upgrade_exit_code_supports_strict_review(self) -> None:
        self.assertEqual(upgrade.exit_code({"status": "ready"}), 0)
        self.assertEqual(upgrade.exit_code({"status": "review_required"}), 0)
        self.assertEqual(
            upgrade.exit_code({"status": "review_required"}, strict=True),
            2,
        )
        self.assertEqual(upgrade.exit_code({"status": "blocked"}), 2)


if __name__ == "__main__":
    unittest.main()
