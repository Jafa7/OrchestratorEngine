from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import cli, continuity, core
from orchestrator_engine import operational_feedback as feedback


class OperationalFeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for actor in ("owner", "developer"):
            continuity.register_actor(
                self.root,
                actor_id=actor,
                role=actor,
                host="codex",
                target_thread_id=f"thread-{actor}",
                completion_delivery_mode="off",
            )
        continuity.start_work(
            self.root, work_id="WORK-1", owner_actor="owner", objective="Test"
        )
        self.details = {
            "observation": "Observed missing signal",
            "expected": "One signal",
            "actual": "No signal",
            "impact": "Manual recovery",
            "evidence": "PRIVATE",
        }

    def record(self, **changes):
        return feedback.record(
            self.root,
            cause="missing-signal",
            classification="defect",
            details={**self.details, **changes},
        )

    def configure(self):
        feedback.configure(
            self.root,
            recipient_actor="developer",
            allowed_fields=["observation", "expected", "actual", "impact"],
        )

    def send(self, report_id):
        return feedback.send(
            self.root, report_id=report_id, work_id="WORK-1", sender_actor="owner"
        )

    def test_duplicate_and_supplement_never_send_implicitly(self):
        first = self.record()
        self.assertTrue(self.record()["duplicate"])
        second = self.record(actual="Still absent after recovery")
        self.assertEqual(second["report_id"], first["report_id"])
        self.assertEqual(second["observation_count"], 2)
        self.assertEqual(continuity.status(self.root)["requests"], [])

    def test_export_is_explicit_filtered_fyi_and_idempotent(self):
        report_id = self.record()["report_id"]
        with self.assertRaises(continuity.ContinuityError):
            self.send(report_id)
        self.configure()
        self.assertEqual(self.send(report_id)["status"], "submitted")
        self.record(actual="New local evidence")
        self.assertTrue(self.send(report_id)["idempotent"])
        state = continuity.status(self.root)
        self.assertEqual(len(state["requests"]), 1)
        request = state["requests"][0]
        self.assertFalse(request["requires_reply"])
        self.assertEqual(state["obligations"], [])
        self.assertNotIn("PRIVATE", request["message"]["text"])
        self.assertNotIn("New local evidence", request["message"]["text"])
        self.assertNotIn(str(self.root), request["message"]["text"])

    def test_failure_preserves_frozen_snapshot_and_recovers_once(self):
        self.configure()
        report_id = self.record()["report_id"]
        real_send = continuity.request_send

        def persist_then_fail(*args, **kwargs):
            real_send(*args, **kwargs)
            raise continuity.ContinuityError("synthetic publication failure")

        with mock.patch.object(
            continuity, "request_send", side_effect=persist_then_fail
        ):
            self.assertEqual(self.send(report_id)["status"], "fallback_required")
        self.assertEqual(
            feedback.show(self.root, report_id=report_id)["delivery"]["status"],
            "fallback_required",
        )
        self.assertTrue(self.send(report_id)["idempotent"])
        self.assertEqual(len(continuity.status(self.root)["requests"]), 1)

    def test_retained_send_recovers_after_product_completion(self):
        self.configure()
        report_id = self.record()["report_id"]
        real_send = continuity.request_send

        def persist_then_fail(*args, **kwargs):
            real_send(*args, **kwargs)
            raise continuity.ContinuityError("synthetic publication failure")

        with mock.patch.object(
            continuity, "request_send", side_effect=persist_then_fail
        ):
            self.assertEqual(self.send(report_id)["status"], "fallback_required")
        continuity.checkpoint(
            self.root, work_id="WORK-1", actor_id="owner",
            expected_revision=1, mode="complete", summary="Product finished",
        )
        recovered = self.send(report_id)
        self.assertEqual(recovered["status"], "submitted")
        self.assertTrue(recovered["idempotent"])
        self.assertTrue(self.send(report_id)["idempotent"])
        state = continuity.status(self.root)
        self.assertEqual(len(state["requests"]), 1)
        self.assertEqual(state["works"][0]["mode"], "complete")
        request = state["requests"][0]
        with self.assertRaisesRegex(continuity.ContinuityError, "identity conflicts"):
            continuity.request_send(
                self.root, work_id="WORK-1", request_id=request["request_id"],
                idempotency_key=request["idempotency_key"], sender_actor="owner",
                recipient_actor="developer", return_actor="owner",
                message="Conflicting retained message",
            )
        with self.assertRaisesRegex(continuity.ContinuityError, "immutable"):
            continuity.request_send(
                self.root, work_id="WORK-1", request_id="NEW-REQUEST",
                idempotency_key="NEW-REQUEST", sender_actor="owner",
                recipient_actor="developer", return_actor="owner", message="New",
            )
        continuity.claim(
            self.root, activation_id=request["request_activation_id"],
            actor_id="developer", expected_epoch=1,
        )

    def test_route_cannot_change_during_recovery(self):
        self.configure()
        report_id = self.record()["report_id"]
        self.send(report_id)
        with self.assertRaisesRegex(continuity.ContinuityError, "identity conflicts"):
            feedback.send(
                self.root, report_id=report_id, work_id="OTHER", sender_actor="owner"
            )

    def test_hostile_ids_and_invalid_details_rejected(self):
        with self.assertRaises(core.OrchestratorError):
            feedback.show(self.root, report_id="../escape")
        for details in (
            {},
            {**self.details, "unknown": "value"},
            {**self.details, "actual": []},
            {**self.details, "actual": "x" * 9000},
        ):
            with (
                self.subTest(details=details),
                self.assertRaises(continuity.ContinuityError),
            ):
                feedback.record(
                    self.root,
                    cause="test",
                    classification="defect",
                    details=details,
                )

    def test_future_schema_is_not_mutated(self):
        report_id = self.record()["report_id"]
        path = Path(self.record()["path"])
        value = json.loads(path.read_text())
        value["schema_version"] = 2
        core.atomic_json(path, value)
        before = path.read_bytes()
        with self.assertRaises(continuity.ContinuityError):
            self.send(report_id)
        self.assertEqual(path.read_bytes(), before)

    def test_bounded_storage(self):
        with mock.patch.object(feedback, "MAX_OBSERVATIONS", 2):
            self.record()
            self.record(actual="Second")
            result = self.record(actual="Third")
        self.assertEqual(result["observation_count"], 2)
        self.assertTrue(result["truncated"])

    def test_cli_parses_opt_in_commands(self):
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "continuity",
                "feedback-record",
                "--cause",
                "test",
                "--classification",
                "defect",
                "--details-json",
                json.dumps(self.details),
            ]
        )
        result = cli.run_continuity_command(args, self.root)
        self.assertEqual(result["observation_count"], 1)

    def test_unknown_destination_is_not_authorized(self):
        with self.assertRaisesRegex(
            continuity.ContinuityError, "active explicitly registered"
        ):
            feedback.configure(
                self.root, recipient_actor="missing", allowed_fields=["observation"]
            )

    def test_malformed_artifact_is_rejected_without_mutation(self):
        path = Path(self.record()["path"])
        report = json.loads(path.read_text())
        report["observations"] = "invalid"
        core.atomic_json(path, report)
        before = path.read_bytes()
        with self.assertRaises(continuity.ContinuityError):
            self.send(report["report_id"])
        self.assertEqual(path.read_bytes(), before)

    def test_stop_retains_report_and_suppresses_route_until_resume(self):
        self.configure()
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            state="stopped",
            actor_id="owner",
            reason="Explicit stop",
        )
        report_id = self.record()["report_id"]
        self.send(report_id)
        state = continuity.status(self.root)
        self.assertIsNone(state["requests"][0]["request_activation_id"])
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            state="armed",
            actor_id="owner",
            reason="Explicit resume",
        )
        continuity.reconcile(self.root)
        state = continuity.status(self.root)
        self.assertEqual(len(state["requests"]), 1)
        self.assertIsNotNone(state["requests"][0]["request_activation_id"])


if __name__ == "__main__":
    unittest.main()
