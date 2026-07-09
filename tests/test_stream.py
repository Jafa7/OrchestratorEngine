from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestrator_engine import binding, claude_stream, core, watcher


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
            binding.write_binding(root, host="claude")
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
            binding.write_binding(root, host="claude")
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

    def test_stream_uses_separate_default_state_and_skips_codex_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = root / "result.json"
            evidence = root / "evidence.json"
            result.write_text('{"status":"ok"}', encoding="utf-8")
            evidence.write_text('{"review_ready":true}', encoding="utf-8")
            core.write_terminal_event(
                root,
                task_id="TASK-CODEX",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-codex",
                wake_target={
                    "schema_version": 1,
                    "kind": binding.WAKE_TARGET_KIND,
                    "host": "codex",
                    "target_thread_id": "thread-codex",
                    "captured_at": "2026-07-08T00:00:00.000+00:00",
                },
            )
            lines: list[str] = []
            stream_state = watcher.default_stream_state_path(root, host="claude")
            claude_stream.stream_signals([root], emit=lines.append, max_scans=1)
            callback_state = root / "callback-state.json"
            callback = watcher.scan_once(
                [root],
                state_path=callback_state,
                action="callback",
                host_adapters={
                    "codex": lambda _project, signal, **_kwargs: {
                        "status": "woken",
                        "event_id": signal["event_id"],
                    }
                },
            )
            self.assertEqual(lines, [])
            self.assertEqual(callback["new_count"], 1)
            self.assertTrue(stream_state.is_file())

    def test_stream_status_reports_fresh_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="claude")
            claude_stream.stream_signals([root], max_scans=1)
            status = claude_stream.stream_status([root])
        self.assertEqual(status["status"], "fresh")
        self.assertTrue(status["healthy"])
        self.assertEqual(status["pending_inbox_count"], 0)

    def test_stream_status_reports_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = watcher.default_stream_state_path(root, host="claude")
            core.atomic_json(
                state,
                {
                    "schema_version": 1,
                    "seen_event_ids": [],
                    "deferred_events": {},
                    "acknowledged_events": {},
                    "updated_at": (
                        datetime.now(UTC) - timedelta(minutes=10)
                    ).isoformat(timespec="milliseconds"),
                },
            )
            status = claude_stream.stream_status([root], interval_seconds=2)
        self.assertEqual(status["status"], "stale")
        self.assertFalse(status["healthy"])

    def test_stream_status_reports_persistent_error_as_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            def always_failing_scan(*_args, **_kwargs):
                raise watcher.WatcherError("stream keeps failing")

            claude_stream.stream_signals(
                [root],
                max_scans=1,
                scan=always_failing_scan,
            )
            status = claude_stream.stream_status([root])
        self.assertEqual(status["status"], "erroring")
        self.assertFalse(status["healthy"])
        self.assertIn("stream keeps failing", status["last_error"])

    def test_stream_records_scan_error_and_keeps_loop_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            calls = {"count": 0}

            def flaky_scan(*args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise watcher.WatcherError("temporary stream failure")
                return watcher.scan_once(*args, **kwargs)

            claude_stream.stream_signals(
                [root],
                max_scans=2,
                sleep=lambda _seconds: None,
                scan=flaky_scan,
            )
            status = claude_stream.stream_status([root])
        self.assertEqual(calls["count"], 2)
        self.assertEqual(status["status"], "fresh")
        self.assertIsNone(status["last_error"])


if __name__ == "__main__":
    unittest.main()
