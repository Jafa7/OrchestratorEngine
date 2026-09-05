from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import binding, core, wakeup, watcher, workstreams


class WorkstreamTests(unittest.TestCase):
    def start(self, root: Path, **kwargs):
        binding.write_binding(root, host="codex", target_thread_id="thread-1")
        return workstreams.start_workstream(root, **kwargs)

    def test_continue_writes_delayed_followup_for_start_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding.write_binding(root, host="codex", target_thread_id="thread-1")
            self.start(
                root,
                workstream_id="ROADMAP-1",
                goal="Finish the accepted roadmap.",
                delay_seconds=60,
            )
            binding.write_binding(root, host="codex", target_thread_id="thread-2")

            result = workstreams.checkpoint_workstream(
                root,
                workstream_id="ROADMAP-1",
                checkpoint_id="phase-1",
                decision="continue",
                summary="Phase one is verified.",
                next_action="Implement phase two.",
                ready=True,
            )
            signal = core.load_object(Path(result["signal_path"]))
            scan = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="record",
            )

        self.assertEqual(signal["wake_target"]["target_thread_id"], "thread-1")
        self.assertIn("not_before", signal)
        self.assertEqual(scan["new_count"], 0)

    def test_start_requires_a_bound_host_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(binding.BindingError, "no binding"):
                workstreams.start_workstream(root, workstream_id="W", goal="Goal")

    def test_zero_delay_followup_is_immediately_due(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(
                root,
                workstream_id="W",
                goal="Goal",
                delay_seconds=0,
            )
            checkpoint = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )
            checkpoint_exists = Path(checkpoint["checkpoint_path"]).is_file()
            scan = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="record",
            )

        self.assertTrue(checkpoint_exists)
        self.assertEqual(scan["new_count"], 1)

    def test_continue_requires_explicit_ready_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            with self.assertRaisesRegex(workstreams.WorkstreamError, "--ready"):
                workstreams.checkpoint_workstream(
                    root,
                    workstream_id="W",
                    checkpoint_id="C1",
                    decision="continue",
                    summary="Ready",
                    next_action="Continue",
                )

    def test_checkpoint_id_is_idempotent_but_content_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            first = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="paused",
                summary="Paused intentionally.",
            )
            second = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="paused",
                summary="Paused intentionally.",
            )
            with self.assertRaisesRegex(
                workstreams.WorkstreamError, "different content"
            ):
                workstreams.checkpoint_workstream(
                    root,
                    workstream_id="W",
                    checkpoint_id="C1",
                    decision="blocked",
                    summary="Different",
                )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])

    def test_continuation_limit_transitions_to_needs_user_without_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(
                root,
                workstream_id="W",
                goal="Goal",
                max_continuations=1,
            )
            first = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="continue",
                summary="First phase complete.",
                next_action="Second phase.",
                ready=True,
            )
            second = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C2",
                decision="continue",
                summary="Second phase complete.",
                next_action="Third phase.",
                ready=True,
            )
            status = workstreams.workstream_status(root, workstream_id="W")

        self.assertIn("followup", first)
        self.assertNotIn("followup", second)
        self.assertEqual(second["decision"], "needs_user")
        self.assertEqual(status["workstreams"][0]["status"], "needs_user")

    def test_waiting_external_consumes_the_same_automatic_resume_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(
                root,
                workstream_id="W",
                goal="Goal",
                max_continuations=1,
            )
            first = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="waiting_external",
                summary="Waiting for the first check.",
                waiting_on="local_check:check-1",
            )
            workstreams.resume_workstream(root, workstream_id="W")
            second = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C2",
                decision="waiting_external",
                summary="Trying to wait for another check.",
                waiting_on="local_check:check-2",
            )
            descriptor = workstreams.load_workstream(root, "W")

        self.assertEqual(first["decision"], "waiting_external")
        self.assertEqual(second["decision"], "needs_user")
        self.assertEqual(descriptor["continuation_count"], 1)
        self.assertEqual(descriptor["status"], "needs_user")

    def test_stop_checkpoint_revokes_an_undelivered_continuation(self) -> None:
        for stop_decision in ("paused", "needs_user", "complete"):
            with self.subTest(stop_decision=stop_decision):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary).resolve()
                    self.start(
                        root,
                        workstream_id="W",
                        goal="Goal",
                        delay_seconds=0,
                    )
                    continuation = workstreams.checkpoint_workstream(
                        root,
                        workstream_id="W",
                        checkpoint_id="C1",
                        decision="continue",
                        summary="Phase complete.",
                        next_action="Start the next phase.",
                        ready=True,
                    )
                    workstreams.checkpoint_workstream(
                        root,
                        workstream_id="W",
                        checkpoint_id="C2",
                        decision=stop_decision,
                        summary="Stop autonomous continuation.",
                    )
                    scan = watcher.scan_once(
                        [root],
                        state_path=root / "watcher-state.json",
                        action="record",
                    )

                self.assertEqual(scan["new_count"], 0)
                self.assertEqual(
                    scan["suppressed_signals"][0]["event_id"],
                    continuation["event_id"],
                )
                self.assertEqual(
                    scan["suppressed_signals"][0]["reason"],
                    "workstream_continuation_revoked",
                )

    def test_expired_continuation_is_suppressed_and_stops_the_workstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(
                root,
                workstream_id="W",
                goal="Goal",
                delay_seconds=0,
                max_wall_seconds=1,
            )
            continuation = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="continue",
                summary="Phase complete.",
                next_action="Start the next phase.",
                ready=True,
            )
            descriptor_path = workstreams.descriptor_path(root, "W")
            descriptor = core.load_object(descriptor_path)
            descriptor["created_at"] = "2000-01-01T00:00:00+00:00"
            core.atomic_json(descriptor_path, descriptor)
            scan = watcher.scan_once(
                [root],
                state_path=root / "watcher-state.json",
                action="record",
            )
            stopped = workstreams.load_workstream(root, "W")

        self.assertEqual(scan["new_count"], 0)
        self.assertEqual(
            scan["suppressed_signals"][0]["event_id"],
            continuation["event_id"],
        )
        self.assertEqual(stopped["status"], "needs_user")

    def test_reconcile_recovers_failure_before_descriptor_update_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal", delay_seconds=0)
            descriptor_path = workstreams.descriptor_path(root, "W")
            original_atomic_json = core.atomic_json
            failed = False

            def fail_descriptor_once(path, value):
                nonlocal failed
                if (
                    Path(path) == descriptor_path
                    and value.get("checkpoint_count") == 1
                    and not failed
                ):
                    failed = True
                    raise OSError("simulated descriptor interruption")
                return original_atomic_json(path, value)

            with mock.patch.object(
                core, "atomic_json", side_effect=fail_descriptor_once
            ), self.assertRaisesRegex(OSError, "descriptor interruption"):
                workstreams.checkpoint_workstream(
                    root,
                    workstream_id="W",
                    checkpoint_id="C1",
                    decision="continue",
                    summary="Ready.",
                    next_action="Continue.",
                    ready=True,
                )

            workstreams.reconcile_workstreams(root)
            recovered = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )
            descriptor = workstreams.load_workstream(root, "W")
            inbox_count = len(core.inbox(root))

        self.assertTrue(recovered["idempotent"])
        self.assertEqual(descriptor["continuation_count"], 1)
        self.assertEqual(inbox_count, 1)

    def test_new_checkpoint_reconciles_interrupted_descriptor_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal", delay_seconds=0)
            descriptor_path = workstreams.descriptor_path(root, "W")
            original_atomic_json = core.atomic_json
            failed = False

            def fail_descriptor_once(path, value):
                nonlocal failed
                if (
                    Path(path) == descriptor_path
                    and value.get("checkpoint_count") == 1
                    and not failed
                ):
                    failed = True
                    raise OSError("simulated descriptor interruption")
                return original_atomic_json(path, value)

            with mock.patch.object(
                core, "atomic_json", side_effect=fail_descriptor_once
            ), self.assertRaisesRegex(OSError, "descriptor interruption"):
                workstreams.checkpoint_workstream(
                    root,
                    workstream_id="W",
                    checkpoint_id="C1",
                    decision="continue",
                    summary="Ready.",
                    next_action="Continue.",
                    ready=True,
                )

            second = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C2",
                decision="paused",
                summary="Pause after recovery.",
            )
            reconciled = workstreams.reconcile_workstreams(root)

        self.assertEqual(second["sequence"], 2)
        self.assertEqual(reconciled[0]["status"], "paused")

    def test_reconcile_recovers_failure_after_descriptor_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal", delay_seconds=0)
            with mock.patch.object(
                core,
                "write_followup_event",
                side_effect=OSError("simulated event interruption"),
            ), self.assertRaisesRegex(OSError, "event interruption"):
                workstreams.checkpoint_workstream(
                    root,
                    workstream_id="W",
                    checkpoint_id="C1",
                    decision="continue",
                    summary="Ready.",
                    next_action="Continue.",
                    ready=True,
                )

            descriptor = workstreams.load_workstream(root, "W")
            self.assertEqual(descriptor["continuation_count"], 1)
            self.assertEqual(core.inbox(root), [])
            workstreams.reconcile_workstreams(root)
            inbox_count = len(core.inbox(root))

        self.assertEqual(inbox_count, 1)

    def test_continuation_event_identity_is_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for workstream_id in ("W.A", "W"):
                self.start(root, workstream_id=workstream_id, goal="Goal")
            first = workstreams.checkpoint_workstream(
                root,
                workstream_id="W.A",
                checkpoint_id="C",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )
            second = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="A.C",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )

        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_checkpoint_and_generated_artifact_namespaces_do_not_collide(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            dotted = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C.result",
                decision="paused",
                summary="A valid dotted checkpoint.",
            )
            workstreams.resume_workstream(root, workstream_id="W")
            continuation = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )
            dotted_value = core.load_object(Path(dotted["checkpoint_path"]))
            event = core.load_object(Path(continuation["event_path"]))
            result_path = Path(event["result_path"])
            reconciled = workstreams.reconcile_workstreams(root)

        self.assertEqual(dotted_value["checkpoint_id"], "C.result")
        self.assertEqual(result_path.parent.name, "results")
        self.assertEqual(reconciled[0]["status"], "active")

    def test_continuation_wakeup_requires_current_state_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            continuation = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )
            signal = core.load_object(Path(continuation["signal_path"]))
            event = core.load_object(Path(continuation["event_path"]))
            message = wakeup.build_wakeup_message(root, signal, event)

        self.assertIn("current active_continuation", message)

    def test_resume_does_not_emit_a_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="needs_user",
                summary="A decision is required.",
            )
            resumed = workstreams.resume_workstream(root, workstream_id="W")

        self.assertEqual(resumed["status"], "active")
        self.assertEqual(core.inbox(root), [])

    def test_resume_does_not_restore_a_revoked_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal", delay_seconds=0)
            continuation = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )
            workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C2",
                decision="paused",
                summary="Pause.",
            )
            workstreams.resume_workstream(root, workstream_id="W")
            scan = watcher.scan_once(
                [root], state_path=root / "watcher-state.json", action="record"
            )

        self.assertEqual(scan["new_count"], 0)
        self.assertEqual(
            scan["suppressed_signals"][0]["event_id"], continuation["event_id"]
        )

    def test_legacy_continuation_authorization_is_migrated_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal", delay_seconds=0)
            continuation = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )
            path = workstreams.descriptor_path(root, "W")
            descriptor = core.load_object(path)
            descriptor.pop("active_continuation")
            descriptor.pop("continuation_state_version")
            core.atomic_json(path, descriptor)

            workstreams.reconcile_workstreams(root)
            migrated = workstreams.load_workstream(root, "W")
            scan = watcher.scan_once(
                [root], state_path=root / "watcher-state.json", action="record"
            )

        self.assertEqual(
            migrated["active_continuation"]["event_id"], continuation["event_id"]
        )
        self.assertEqual(migrated["continuation_state_version"], 1)
        self.assertEqual(scan["new_count"], 1)

    def test_expired_signal_cannot_reopen_a_completed_workstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(
                root,
                workstream_id="W",
                goal="Goal",
                delay_seconds=0,
                max_wall_seconds=1,
            )
            workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="continue",
                summary="Ready.",
                next_action="Continue.",
                ready=True,
            )
            workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C2",
                decision="complete",
                summary="Done.",
            )
            path = workstreams.descriptor_path(root, "W")
            descriptor = core.load_object(path)
            descriptor["created_at"] = "2000-01-01T00:00:00+00:00"
            core.atomic_json(path, descriptor)

            scan = watcher.scan_once(
                [root], state_path=root / "watcher-state.json", action="record"
            )
            completed = workstreams.load_workstream(root, "W")

        self.assertEqual(scan["new_count"], 0)
        self.assertEqual(
            scan["suppressed_signals"][0]["reason"],
            "workstream_continuation_revoked",
        )
        self.assertEqual(completed["status"], "complete")

    def test_reconciliation_cannot_apply_checkpoint_after_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            completed = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="complete",
                summary="Done.",
            )
            invalid = {
                **completed,
                "checkpoint_id": "C2",
                "sequence": 2,
                "decision": "paused",
                "requested_decision": "paused",
                "summary": "Must not reopen.",
            }
            invalid.pop("checkpoint_path")
            invalid.pop("idempotent")
            core.atomic_json(
                workstreams.checkpoint_path(root, "W", "C2"), invalid
            )

            reconciled = workstreams.reconcile_workstreams(root)
            descriptor = workstreams.load_workstream(root, "W")

        self.assertEqual(reconciled[0]["status"], "error")
        self.assertIn("completed workstream", reconciled[0]["error"])
        self.assertEqual(descriptor["status"], "complete")

    def test_reconciliation_isolates_a_broken_workstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="A-broken", goal="Broken")
            self.start(root, workstream_id="Z-valid", goal="Valid", delay_seconds=0)
            broken = (
                workstreams.workstream_dir(root, "A-broken")
                / "checkpoints"
                / "bad.json"
            )
            core.atomic_json(broken, {"kind": "INVALID"})
            with mock.patch.object(
                core,
                "write_followup_event",
                side_effect=OSError("simulated event interruption"),
            ), self.assertRaisesRegex(OSError, "event interruption"):
                workstreams.checkpoint_workstream(
                    root,
                    workstream_id="Z-valid",
                    checkpoint_id="C1",
                    decision="continue",
                    summary="Ready.",
                    next_action="Continue.",
                    ready=True,
                )

            scan = watcher.scan_once(
                [root], state_path=root / "watcher-state.json", action="record"
            )

        reconciled = scan["workstream_reconciliations"]
        self.assertEqual(reconciled[0]["workstream_id"], "A-broken")
        self.assertEqual(reconciled[0]["status"], "error")
        self.assertEqual(reconciled[1]["workstream_id"], "Z-valid")
        self.assertEqual(reconciled[1]["recovered_events"], 1)
        self.assertEqual(scan["new_count"], 1)

    def test_active_workstream_cannot_be_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            with self.assertRaisesRegex(workstreams.WorkstreamError, "does not need"):
                workstreams.resume_workstream(root, workstream_id="W")

    def test_continue_after_needs_user_requires_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C1",
                decision="needs_user",
                summary="A decision is required.",
            )
            with self.assertRaisesRegex(workstreams.WorkstreamError, "resume"):
                workstreams.checkpoint_workstream(
                    root,
                    workstream_id="W",
                    checkpoint_id="C2",
                    decision="continue",
                    summary="Trying to continue.",
                    next_action="Unsafe continuation.",
                    ready=True,
                )

    def test_waiting_external_requires_operation_identity_and_emits_no_signal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.start(root, workstream_id="W", goal="Goal")
            with self.assertRaisesRegex(workstreams.WorkstreamError, "--waiting-on"):
                workstreams.checkpoint_workstream(
                    root,
                    workstream_id="W",
                    checkpoint_id="C1",
                    decision="waiting_external",
                    summary="Waiting for CI.",
                )
            checkpoint = workstreams.checkpoint_workstream(
                root,
                workstream_id="W",
                checkpoint_id="C2",
                decision="waiting_external",
                summary="Waiting for CI.",
                waiting_on="github-actions:run-123",
            )

        self.assertEqual(checkpoint["waiting_on"], "github-actions:run-123")
        self.assertEqual(core.inbox(root), [])

    def test_status_reports_invalid_descriptor_instead_of_hiding_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = workstreams.descriptor_path(root, "W")
            core.atomic_json(path, {"schema_version": 1, "kind": "WRONG"})
            report = workstreams.workstream_status(root)

        self.assertEqual(report["workstream_count"], 0)
        self.assertEqual(report["invalid_count"], 1)
        self.assertEqual(report["invalid"][0]["path"], str(path))
