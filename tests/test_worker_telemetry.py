from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestrator_engine import core, task_diagnostics, telemetry_adapters, workers


class WorkerTelemetryTests(unittest.TestCase):
    def test_json_lines_usage_adapter_is_explicit_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            stdout.write_text(
                json.dumps({"usage": {"input_tokens": 10, "output_tokens": 4}})
                + "\nnot json\n",
                encoding="utf-8",
            )
            stderr.write_text("", encoding="utf-8")
            usage = telemetry_adapters.collect("json-lines-usage", stdout, stderr)

        self.assertEqual(usage["total_tokens"], 14)
        self.assertEqual(usage["parsed_records"], 1)

    def test_supervisor_records_optional_handoff_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            script = root / "worker.py"
            script.write_text(
                """
import json, os, pathlib, re, sys
prompt = sys.stdin.read()
match = re.search(r"path: (.+)", prompt)
pathlib.Path(match.group(1)).write_text(json.dumps({
    "schema_version": 1,
    "kind": "WORKER_HANDOFF",
    "summary": "done",
    "evidence": ["ok"]
}))
output = pathlib.Path(os.environ["ORCHESTRATOR_DECLARED_OUTPUT_DIR"])
(output / "full-plan.md").write_text("complete durable plan")
print(json.dumps({"usage": {"input_tokens": 8, "output_tokens": 3}}))
""",
                encoding="utf-8",
            )
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                f"""
[workers.capture]
command = ["{sys.executable}", "{script}"]
prompt_via = "stdin"
usage_adapter = "json-lines-usage"
soft_token_budget = 5
""",
                encoding="utf-8",
            )
            prompt = root / "prompt.md"
            prompt.write_text("work", encoding="utf-8")
            dispatched = workers.run_worker(
                root,
                worker="capture",
                task_id="T-USAGE",
                prompt_file=prompt,
            )
            task_dir = Path(dispatched["task_dir"])
            deadline = datetime.now(UTC) + timedelta(seconds=8)
            while True:
                if (task_dir / "task.json").is_file():
                    current = core.load_object(task_dir / "task.json")
                    if current.get("status") == "completed":
                        time.sleep(0.1)
                        break
                if datetime.now(UTC) >= deadline:
                    self.fail("worker did not finish")
                time.sleep(0.05)
            result = core.load_object(task_dir / "result.json")
            descriptor = core.load_object(task_dir / "task.json")
            output_manifest = core.load_object(task_dir / "worker-outputs.json")
            report = task_diagnostics.diagnose_tasks(
                root, task_id="T-USAGE", minimum_severity="info"
            )

        self.assertEqual(result["usage"]["total_tokens"], 11)
        self.assertEqual(descriptor["worker_handoff"]["bytes"] > 0, True)
        self.assertEqual(output_manifest["file_count"], 1)
        self.assertEqual(output_manifest["files"][0]["path"], "outputs/full-plan.md")
        codes = {item["code"] for item in report["tasks"]["T-USAGE"]["diagnostics"]}
        self.assertIn("task_soft_token_budget_exceeded", codes)


if __name__ == "__main__":
    unittest.main()
