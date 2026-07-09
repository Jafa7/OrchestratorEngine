from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_engine import binding, core, diagnostics, workers


class DiagnosticsTests(unittest.TestCase):
    def test_doctor_warns_when_state_layout_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = diagnostics.run_doctor(root)

        layout = check_by_name(report, "state_layout")
        self.assertEqual(layout["status"], "warn")
        self.assertEqual(report["status"], "warn")

    def test_doctor_errors_on_unsupported_future_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core.atomic_json(
                core.events_root(root) / "future.json",
                {
                    "schema_version": 999,
                    "kind": "WORKER_TERMINAL",
                    "event_id": "future",
                },
            )
            report = diagnostics.run_doctor(root)

        schema = check_by_name(report, "schema_compatibility")
        self.assertEqual(schema["status"], "error")
        self.assertEqual(report["status"], "error")

    def test_doctor_errors_on_unsupported_old_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core.atomic_json(
                core.events_root(root) / "old.json",
                {
                    "schema_version": 0,
                    "kind": "WORKER_TERMINAL",
                    "event_id": "old",
                },
            )
            report = diagnostics.run_doctor(root)

        schema = check_by_name(report, "schema_compatibility")
        self.assertEqual(schema["status"], "error")
        self.assertEqual(schema["data"]["incompatible"][0]["schema_version"], 0)

    def test_doctor_warns_on_malformed_schema_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core.atomic_json(
                core.events_root(root) / "malformed.json",
                {
                    "kind": "WORKER_TERMINAL",
                    "event_id": "malformed",
                },
            )
            report = diagnostics.run_doctor(root)

        schema = check_by_name(report, "schema_compatibility")
        self.assertEqual(schema["status"], "warn")
        self.assertEqual(schema["data"]["malformed"][0]["schema_version"], None)

    def test_doctor_warns_when_binding_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = diagnostics.run_doctor(root)

        self.assertEqual(check_by_name(report, "binding")["status"], "warn")

    def test_doctor_errors_on_invalid_binding_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core.atomic_json(
                binding.binding_path(root),
                {
                    "schema_version": 999,
                    "kind": binding.BINDING_KIND,
                    "host": "claude",
                },
            )
            report = diagnostics.run_doctor(root)

        self.assertEqual(check_by_name(report, "binding")["status"], "error")

    def test_doctor_errors_on_non_integer_binding_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core.atomic_json(
                binding.binding_path(root),
                {
                    "schema_version": True,
                    "kind": binding.BINDING_KIND,
                    "host": "claude",
                },
            )
            report = diagnostics.run_doctor(root)

        self.assertEqual(check_by_name(report, "binding")["status"], "error")

    def test_doctor_warns_when_no_enabled_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = workers.workers_config_path(root)
            config.parent.mkdir(parents=True)
            config.write_text(
                "\n".join(
                    [
                        "[workers.disabled]",
                        "enabled = false",
                        'command = ["true"]',
                    ]
                ),
                encoding="utf-8",
            )
            report = diagnostics.run_doctor(root)

        workers_check = check_by_name(report, "workers")
        self.assertEqual(workers_check["status"], "warn")
        self.assertEqual(workers_check["data"]["enabled_count"], 0)

    def test_doctor_reports_watcher_not_started_for_codex_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            report = diagnostics.run_doctor(root)

        channel = check_by_name(report, "watcher_channel")
        self.assertEqual(channel["status"], "warn")
        self.assertEqual(channel["data"]["service_status"]["status"], "not_started")

    def test_doctor_warns_on_claude_stream_not_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="claude")
            report = diagnostics.run_doctor(root)

        channel = check_by_name(report, "watcher_channel")
        self.assertEqual(channel["status"], "warn")
        self.assertEqual(channel["data"]["stream_status"]["status"], "not_started")

    def test_doctor_uses_host_scoped_status_for_vscode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="vscode")
            report = diagnostics.run_doctor(root)

        channel = check_by_name(report, "watcher_channel")
        self.assertEqual(channel["status"], "warn")
        service = channel["data"]["service_status"]
        self.assertEqual(service["status"], "not_started")
        self.assertTrue(
            service["service_file"].endswith("watcher-vscode-callback-service.json")
        )

    def test_doctor_warns_when_engine_not_installed_as_distribution(self) -> None:
        with patch(
            "orchestrator_engine.diagnostics.importlib.metadata.version",
            side_effect=diagnostics.importlib.metadata.PackageNotFoundError,
        ):
            result = diagnostics.check_engine_import()

        self.assertEqual(result["status"], "warn")

    def test_doctor_exit_code_policy(self) -> None:
        self.assertEqual(diagnostics.doctor_exit_code({"status": "ok"}), 0)
        self.assertEqual(diagnostics.doctor_exit_code({"status": "warn"}), 0)
        self.assertEqual(
            diagnostics.doctor_exit_code({"status": "warn"}, strict=True),
            2,
        )
        self.assertEqual(diagnostics.doctor_exit_code({"status": "error"}), 2)

    def test_doctor_does_not_create_state_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            diagnostics.run_doctor(root)
            state_exists = core.state_root(root).exists()

        self.assertFalse(state_exists)


def check_by_name(report: dict, name: str) -> dict:
    for item in report["checks"]:
        if item["name"] == name:
            return item
    raise AssertionError(f"missing check: {name}")


if __name__ == "__main__":
    unittest.main()
