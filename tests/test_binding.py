from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import binding


class BindingTests(unittest.TestCase):
    def test_write_and_load_codex_binding_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            written = binding.write_binding(
                root,
                host="codex",
                target_thread_id="thread-1",
            )
            loaded = binding.load_binding(root)
        self.assertEqual(written["host"], "codex")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["target_thread_id"], "thread-1")
        self.assertEqual(loaded["kind"], binding.BINDING_KIND)

    def test_codex_binding_requires_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(binding.BindingError):
                binding.write_binding(root, host="codex")

    def test_codex_binding_stores_codex_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(
                root,
                host="codex",
                target_thread_id="thread-1",
                codex_command="/mnt/c/apps/codex.exe",
            )
            loaded = binding.load_binding(root)
        self.assertEqual(loaded["codex_command"], "/mnt/c/apps/codex.exe")

    def test_vscode_binding_needs_no_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            written = binding.write_binding(root, host="vscode")
        self.assertEqual(written["host"], "vscode")
        self.assertNotIn("target_thread_id", written)

    def test_unsupported_host_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(binding.BindingError):
                binding.write_binding(root, host="emacs")

    def test_require_binding_raises_without_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(binding.BindingError):
                binding.require_binding(root)

    def test_clear_binding_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="claude")
            cleared = binding.clear_binding(root)
            reloaded = binding.load_binding(root)
        self.assertEqual(cleared["status"], "cleared")
        self.assertIsNone(reloaded)


if __name__ == "__main__":
    unittest.main()
