from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import adoption, binding, core, workers


class AdoptionTests(unittest.TestCase):
    def test_adopt_creates_state_layout_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = adoption.adopt_project(root, host="codex")
            events_exists = core.events_root(root).is_dir()
            signals_exists = (core.inbox_root(root) / "signals").is_dir()
            config_exists = workers.workers_config_path(root).is_file()

        self.assertEqual(result["status"], "created")
        self.assertTrue(events_exists)
        self.assertTrue(signals_exists)
        self.assertTrue(config_exists)
        self.assertIn("worker list", " ".join(result["next_steps"]))

    def test_adopt_is_idempotent_on_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = adoption.adopt_project(root)
            second = adoption.adopt_project(root)

        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "already_present")
        self.assertFalse(second["created"])

    def test_adopt_does_not_overwrite_existing_workers_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            original = '[workers.keep]\ncommand = ["true"]\n'
            config.write_text(original, encoding="utf-8")
            adoption.adopt_project(root)
            content = config.read_text(encoding="utf-8")

        self.assertEqual(content, original)

    def test_adopt_skips_invalid_existing_workers_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            original = "not valid = ["
            config.write_text(original, encoding="utf-8")
            result = adoption.adopt_project(root)
            content = config.read_text(encoding="utf-8")

        self.assertEqual(content, original)
        self.assertIn(".orchestrator/workers.toml", result["skipped"])

    def test_adopt_preserves_existing_durable_events_and_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            event = core.events_root(root) / "event-1.json"
            signal = core.inbox_root(root) / "signals" / "event-1.json"
            core.atomic_json(event, {"schema_version": 1, "kind": "WORKER_TERMINAL"})
            core.atomic_json(
                signal,
                {"schema_version": 1, "kind": "LOCAL_AI_WORKER_FINISHED"},
            )
            adoption.adopt_project(root)
            event_exists = event.is_file()
            signal_exists = signal.is_file()

        self.assertTrue(event_exists)
        self.assertTrue(signal_exists)

    def test_adopt_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = adoption.adopt_project(root, dry_run=True)
            state_exists = core.state_root(root).exists()

        self.assertEqual(result["status"], "created")
        self.assertFalse(state_exists)

    def test_adopt_never_writes_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            adoption.adopt_project(root, host="claude")
            binding_exists = binding.binding_path(root).exists()

        self.assertFalse(binding_exists)

    def test_adopt_next_steps_reflect_host_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            claude = adoption.adopt_project(root, host="claude")

        self.assertIn("watcher stream", " ".join(claude["next_steps"]))

    def test_adopt_refuses_home_and_filesystem_root(self) -> None:
        with self.assertRaises(adoption.AdoptionError):
            adoption.adopt_project(Path.home())
        with self.assertRaises(adoption.AdoptionError):
            adoption.adopt_project(Path("/"))

    def test_adopt_refuses_state_dir_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for state_dir in ("/tmp/orchestrator-outside", "../outside", ""):
                with (
                    self.subTest(state_dir=state_dir),
                    self.assertRaises(adoption.AdoptionError),
                ):
                    adoption.adopt_project(root, state_dir=state_dir)


if __name__ == "__main__":
    unittest.main()
