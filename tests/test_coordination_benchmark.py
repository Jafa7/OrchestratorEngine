from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "benchmarks" / "coordination_context.py"


class CoordinationContextBenchmarkTests(unittest.TestCase):
    def test_benchmark_keeps_full_logs_addressable_and_reduces_context(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as temporary:
            svg_path = Path(temporary) / "coordination-context.svg"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--write-svg", str(svg_path)],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                svg_path.read_text(encoding="utf-8"),
                (ROOT / "docs/assets/coordination-context.svg").read_text(
                    encoding="utf-8"
                ),
            )
        report = json.loads(completed.stdout)
        published = json.loads(
            (ROOT / "docs/assets/coordination-context.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(report, published)
        self.assertEqual(
            report["kind"],
            "ORCHESTRATOR_COORDINATION_CONTEXT_BENCHMARK",
        )
        self.assertEqual(len(report["scenarios"]), 3)
        for scenario in report["scenarios"]:
            self.assertEqual(scenario["quality_guard"], "passed")
            self.assertEqual(scenario["poll_count"], 4)
            self.assertGreater(scenario["naive_polling_bytes"], 0)
            self.assertGreater(scenario["orchestrator_status_bytes"], 0)
            self.assertLess(
                scenario["orchestrator_status_bytes"],
                scenario["naive_polling_bytes"],
            )
            self.assertGreater(scenario["context_reduction_percent"], 0)


if __name__ == "__main__":
    unittest.main()
