from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import core, task_resolution, workers


def write_task(root: Path, task_id: str, *, status: str = "failed") -> Path:
    task_dir = workers.task_dir_for(root, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    core.atomic_json(task_dir / "result.json", {"terminal_status": status})
    core.atomic_json(task_dir / "evidence.json", {"ok": True})
    core.atomic_json(
        task_dir / "task.json",
        {
            "schema_version": 1,
            "kind": workers.TASK_KIND,
            "task_id": task_id,
            "worker": "echo",
            "status": status,
        },
    )
    return task_dir


class TaskResolutionTests(unittest.TestCase):
    def test_write_and_load_acknowledged_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(root, "T-FAIL")

            written = task_resolution.write_resolution(
                root,
                task_id="T-FAIL",
                status="acknowledged",
                reason="Reviewed manually; no follow-up needed.",
            )
            loaded = task_resolution.load_resolution(root, "T-FAIL")

        self.assertEqual(written["kind"], task_resolution.TASK_RESOLUTION_KIND)
        self.assertEqual(loaded["status"], "acknowledged")
        self.assertEqual(loaded["task_id"], "T-FAIL")
        self.assertIn("resolution_path", loaded)

    def test_superseded_resolution_requires_target_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(root, "T-OLD")
            write_task(root, "T-NEW", status="completed")

            written = task_resolution.write_resolution(
                root,
                task_id="T-OLD",
                status="superseded",
                reason="Successful rerun replaced this failed attempt.",
                superseded_by_task_id="T-NEW",
            )

        self.assertEqual(written["superseded_by_task_id"], "T-NEW")

    def test_superseded_resolution_preserves_scoped_diagnostic_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(root, "T-OLD")
            write_task(root, "T-NEW", status="completed")

            written = task_resolution.write_resolution(
                root,
                task_id="T-OLD",
                status="superseded",
                reason="Successful rerun used corrected worker settings.",
                superseded_by_task_id="T-NEW",
                diagnostic_codes=["copilot_may_request_approval"],
            )

        self.assertEqual(written["status"], "superseded")
        self.assertEqual(written["superseded_by_task_id"], "T-NEW")
        self.assertEqual(
            written["diagnostic_codes"],
            ["copilot_may_request_approval"],
        )

    def test_completed_acknowledgement_requires_diagnostic_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(root, "T-OK", status="completed")

            with self.assertRaises(task_resolution.TaskResolutionError):
                task_resolution.write_resolution(
                    root,
                    task_id="T-OK",
                    status="acknowledged",
                    reason="Completed tasks do not need resolution.",
                )

    def test_completed_task_accepts_scoped_diagnostic_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(root, "T-OK", status="completed")

            written = task_resolution.write_resolution(
                root,
                task_id="T-OK",
                status="acknowledged",
                reason="Complete stdout deliverable inspected.",
                diagnostic_codes=["claude_plan_output_may_be_external"],
            )
            loaded = task_resolution.load_resolution(root, "T-OK")

        self.assertEqual(written["previous_task_status"], "completed")
        self.assertEqual(
            loaded["diagnostic_codes"],
            ["claude_plan_output_may_be_external"],
        )

    def test_superseded_resolution_requires_completed_target_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(root, "T-OLD")
            write_task(root, "T-NEW", status="failed")

            with self.assertRaises(task_resolution.TaskResolutionError):
                task_resolution.write_resolution(
                    root,
                    task_id="T-OLD",
                    status="superseded",
                    reason="Target did not complete.",
                    superseded_by_task_id="T-NEW",
                )

    def test_existing_resolution_requires_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            write_task(root, "T-FAIL")
            task_resolution.write_resolution(
                root,
                task_id="T-FAIL",
                status="acknowledged",
                reason="first",
            )

            with self.assertRaises(task_resolution.TaskResolutionError):
                task_resolution.write_resolution(
                    root,
                    task_id="T-FAIL",
                    status="acknowledged",
                    reason="second",
                )

    def test_list_reports_invalid_resolution_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = task_resolution.resolutions_root(root) / "bad.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")

            report = task_resolution.list_resolutions(root)

        self.assertEqual(report["resolution_count"], 0)
        self.assertEqual(report["invalid_count"], 1)


if __name__ == "__main__":
    unittest.main()
