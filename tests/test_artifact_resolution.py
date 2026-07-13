from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import artifact_resolution, core, diagnostics


class ArtifactResolutionTests(unittest.TestCase):
    def test_hash_bound_resolution_clears_malformed_schema_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            handoff = core.state_root(root) / "tasks" / "T-OLD" / "worker-handoff.json"
            core.atomic_json(
                handoff,
                {"kind": "WORKER_HANDOFF", "summary": "Historical output."},
            )
            original = handoff.read_bytes()

            before = diagnostics.check_schema_compatibility(
                root,
                state_dir=core.DEFAULT_STATE_DIR,
            )
            resolution = artifact_resolution.write_resolution(
                root,
                artifact_path=handoff,
                reason="Known malformed prompt output reviewed after upgrade.",
            )
            after = diagnostics.check_schema_compatibility(
                root,
                state_dir=core.DEFAULT_STATE_DIR,
            )
            preserved = handoff.read_bytes()
            current_hash = core.sha256_file(handoff)

        self.assertEqual(before["status"], "warn")
        self.assertEqual(after["status"], "ok")
        self.assertEqual(after["data"]["unsupported_count"], 0)
        self.assertEqual(after["data"]["resolved_malformed_count"], 1)
        self.assertEqual(diagnostics.doctor_exit_code(after, strict=True), 0)
        self.assertEqual(preserved, original)
        self.assertEqual(resolution["artifact_sha256"], current_hash)

    def test_state_relative_list_path_round_trips_and_reason_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            handoff = core.state_root(root) / "tasks" / "T-OLD" / "worker-handoff.json"
            core.atomic_json(handoff, {"kind": "WORKER_HANDOFF"})
            artifact_resolution.write_resolution(
                root,
                artifact_path=handoff,
                reason="Reviewed exact bytes.",
            )
            listed = artifact_resolution.list_resolutions(root)
            state_relative = listed["resolutions"][0]["artifact_path"]
            repeated = artifact_resolution.write_resolution(
                root,
                artifact_path=state_relative,
                reason="Reviewed exact bytes.",
            )
            with self.assertRaisesRegex(
                artifact_resolution.ArtifactResolutionError,
                "different reason",
            ):
                artifact_resolution.write_resolution(
                    root,
                    artifact_path=state_relative,
                    reason="A different operator conclusion.",
                )

        self.assertTrue(repeated["idempotent"])

    def test_concurrent_resolution_cannot_replace_winning_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            handoff = core.state_root(root) / "tasks" / "T-OLD" / "worker-handoff.json"
            core.atomic_json(handoff, {"kind": "WORKER_HANDOFF"})

            def competing_claim(path: Path, value: object) -> bool:
                self.assertIsInstance(value, dict)
                winner = {**value, "reason": "Concurrent winner."}
                core.atomic_json(path, winner)
                return False

            with (
                mock.patch.object(core, "claim_json", side_effect=competing_claim),
                self.assertRaisesRegex(
                    artifact_resolution.ArtifactResolutionError,
                    "different reason",
                ),
            ):
                artifact_resolution.write_resolution(
                    root,
                    artifact_path=handoff,
                    reason="Concurrent loser.",
                )
            listed = artifact_resolution.list_resolutions(root)

        self.assertEqual(listed["resolutions"][0]["reason"], "Concurrent winner.")

    def test_changed_artifact_invalidates_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            handoff = core.state_root(root) / "tasks" / "T-OLD" / "worker-handoff.json"
            core.atomic_json(handoff, {"kind": "WORKER_HANDOFF", "summary": "old"})
            artifact_resolution.write_resolution(
                root,
                artifact_path=handoff,
                reason="Reviewed old bytes.",
            )
            core.atomic_json(handoff, {"kind": "WORKER_HANDOFF", "summary": "changed"})

            report = diagnostics.check_schema_compatibility(
                root,
                state_dir=core.DEFAULT_STATE_DIR,
            )
            listed = artifact_resolution.list_resolutions(root)

            artifact_resolution.write_resolution(
                root,
                artifact_path=handoff,
                reason="Reviewed changed bytes separately.",
            )
            relisted = artifact_resolution.list_resolutions(root)

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["data"]["resolved_malformed_count"], 0)
        self.assertFalse(listed["resolutions"][0]["active"])
        self.assertEqual(relisted["resolution_count"], 2)
        self.assertEqual(
            sum(item["active"] for item in relisted["resolutions"]),
            1,
        )

    def test_resolution_rejects_real_unsupported_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            event = core.events_root(root) / "future.json"
            core.atomic_json(event, {"schema_version": 999, "kind": "WORKER_TERMINAL"})

            with self.assertRaisesRegex(
                artifact_resolution.ArtifactResolutionError,
                "not currently reported with malformed schema metadata",
            ):
                artifact_resolution.write_resolution(
                    root,
                    artifact_path=event,
                    reason="Must not hide incompatible schemas.",
                )

    def test_invalid_resolution_companion_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            resolution_root = artifact_resolution.resolutions_root(root)
            core.atomic_json(
                resolution_root / "wrong-name.json",
                {
                    "schema_version": 1,
                    "kind": artifact_resolution.ARTIFACT_RESOLUTION_KIND,
                    "artifact_path": "tasks/T-OLD/worker-handoff.json",
                    "artifact_sha256": "a" * 64,
                    "diagnostic_code": artifact_resolution.DIAGNOSTIC_CODE,
                    "reason": "Forged filename must not be accepted.",
                    "created_at": "2026-07-13T00:00:00Z",
                },
            )

            report = diagnostics.check_schema_compatibility(
                root,
                state_dir=core.DEFAULT_STATE_DIR,
            )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["data"]["invalid_artifact_resolution_count"], 1)
        self.assertEqual(report["data"]["unsupported_count"], 0)
        self.assertEqual(report["data"]["malformed"], [])

    def test_symlinked_artifact_cannot_remain_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            handoff = core.state_root(root) / "tasks" / "T-OLD" / "worker-handoff.json"
            core.atomic_json(handoff, {"kind": "WORKER_HANDOFF"})
            content = handoff.read_bytes()
            artifact_resolution.write_resolution(
                root,
                artifact_path=handoff,
                reason="Reviewed exact bytes.",
            )
            outside = root / "outside.json"
            outside.write_bytes(content)
            handoff.unlink()
            handoff.symlink_to(outside)

            listed = artifact_resolution.list_resolutions(root)
            report = diagnostics.check_schema_compatibility(
                root,
                state_dir=core.DEFAULT_STATE_DIR,
            )
            with self.assertRaisesRegex(
                artifact_resolution.ArtifactResolutionError,
                "does not accept symlinks",
            ):
                artifact_resolution.write_resolution(
                    root,
                    artifact_path=handoff,
                    reason="Must not follow external bytes.",
                )

        self.assertFalse(listed["resolutions"][0]["active"])
        self.assertIn("symlink", listed["resolutions"][0]["artifact_error"])
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["data"]["unreadable_count"], 1)


if __name__ == "__main__":
    unittest.main()
