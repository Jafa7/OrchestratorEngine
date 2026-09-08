from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import core, task_diagnostics, workers


class UsageWithoutBudgetTests(unittest.TestCase):
    def test_worker_records_real_usage_without_a_soft_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            script = root / "worker.py"
            script.write_text(
                "import json, sys\nsys.stdin.read()\n"
                'print(json.dumps({"type":"turn.completed",'
                '"usage":{"input_tokens":10,"output_tokens":5}}))\n'
            )
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                "[workers.capture]\ncommand = "
                + json.dumps([sys.executable, str(script)])
                + '\nprompt_via = "stdin"\nusage_adapter = "codex-jsonl-usage"\n'
            )
            prompt = root / "prompt.md"
            prompt.write_text("Accepted work")
            dispatched = workers.run_worker(
                root,
                worker="capture",
                task_id="USAGE",
                prompt_file=prompt,
                wake_policy="never",
            )
            workers.wait_for_worker_task(
                root, task_id="USAGE", timeout_seconds=8, interval_seconds=0.05
            )
            result = core.load_object(Path(dispatched["task_dir"]) / "result.json")
            report = task_diagnostics.diagnose_tasks(
                root, task_id="USAGE", minimum_severity="info"
            )
            self.assertEqual(result["terminal_status"], "completed")
            self.assertEqual(result["usage"]["measurement_status"], "complete")
            self.assertEqual(result["usage"]["total_tokens"], 15)
            self.assertNotIn(
                "task_soft_token_budget_exceeded",
                {d["code"] for d in report["tasks"]["USAGE"]["diagnostics"]},
            )
