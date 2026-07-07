from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import claude_stream, core


def write_event(root: Path, event_id: str) -> None:
    result = root / f"{event_id}-result.json"
    evidence = root / f"{event_id}-evidence.json"
    result.write_text('{"status":"ok"}', encoding="utf-8")
    evidence.write_text('{"review_ready":true}', encoding="utf-8")
    core.write_terminal_event(
        root,
        task_id=f"TASK-{event_id}",
        terminal_status="completed",
        result_path=result,
        evidence_path=evidence,
        event_id=event_id,
    )


class StreamTests(unittest.TestCase):
    def test_stream_emits_one_line_per_new_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_event(root, "event-a")
            write_event(root, "event-b")
            lines: list[str] = []
            claude_stream.stream_signals(
                [root],
                state_path=root / "watcher-state.json",
                emit=lines.append,
                max_scans=1,
            )
        self.assertEqual(len(lines), 2)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(
            {row["event_id"] for row in parsed},
            {"event-a", "event-b"},
        )
        for row in parsed:
            self.assertEqual(row["kind"], "LOCAL_AI_ORCHESTRATOR_SIGNAL")
            self.assertEqual(row["requires"], "ORCHESTRATOR_FOLLOWUP")

    def test_stream_does_not_reemit_seen_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = root / "watcher-state.json"
            write_event(root, "event-a")
            first: list[str] = []
            second: list[str] = []
            claude_stream.stream_signals(
                [root],
                state_path=state,
                emit=first.append,
                max_scans=1,
            )
            claude_stream.stream_signals(
                [root],
                state_path=state,
                emit=second.append,
                max_scans=1,
            )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
