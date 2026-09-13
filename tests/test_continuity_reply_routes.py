from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import continuity


class ReplyRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for actor in ("owner", "reviewer", "other"):
            continuity.register_actor(
                self.root,
                actor_id=actor,
                role=actor,
                host="codex",
                target_thread_id=f"thread-{actor}",
                completion_delivery_mode="off",
            )
        continuity.configure_recovery(
            self.root,
            enabled=getattr(self, "recovery_enabled", True),
            interval_seconds=10,
            max_interval_seconds=40,
        )
        continuity.start_work(
            self.root, work_id="WORK-1", owner_actor="owner", objective="Test"
        )
        self.request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REVIEW-1",
            idempotency_key="REVIEW-1",
            sender_actor="owner",
            recipient_actor="reviewer",
            return_actor="owner",
            message="Review",
            requires_reply=True,
            max_reminders=0,
        )

    def state(self):
        return continuity.status(self.root, work_id="WORK-1")

    def checkpoint(self, **kwargs):
        return continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="owner",
            expected_revision=self.state()["works"][0]["revision"],
            **kwargs,
        )

    def wait(self, sources=None, **kwargs):
        return self.checkpoint(
            mode="waiting",
            summary="Waiting",
            wait_on=sources or ["obligation:" + self.request["obligation_id"]],
            **kwargs,
        )

    def respond(self):
        activation = self.request["request_activation_id"]
        packet = continuity.entry_packet(self.root, activation_id=activation)
        continuity.claim(
            self.root,
            activation_id=activation,
            actor_id="reviewer",
            expected_epoch=packet["activation"]["control_epoch"],
        )
        continuity.request_respond(
            self.root,
            request_id=self.request["request_id"],
            response_id="RESULT-1",
            actor_id="reviewer",
            activation_id=activation,
            kind="terminal",
            terminal_status="completed",
            content="Findings",
        )
        state = self.state()
        request = next(
            x
            for x in state["requests"]
            if x["request_id"] == self.request["request_id"]
        )
        self.reply = next(
            x
            for x in state["activations"]
            if x["activation_id"] == request["reply_activation_id"]
        )
        self.outcome = continuity.entry_packet(
            self.root, activation_id=self.reply["activation_id"]
        )["managed_results"][0]["outcome_id"]

    def handle(self):
        continuity.claim(
            self.root,
            activation_id=self.reply["activation_id"],
            actor_id="owner",
            expected_epoch=self.reply["control_epoch"],
        )
        continuity.request_handle(
            self.root,
            request_id="REVIEW-1",
            actor_id="owner",
            activation_id=self.reply["activation_id"],
        )

    def age_reply(self):
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE activations SET claimed_at=? WHERE activation_id=?",
                ("2000-01-01T00:00:00+00:00", self.reply["activation_id"]),
            )

    def active_owner(self):
        return [
            x
            for x in self.state()["activations"]
            if x["actor_id"] == "owner"
            and x["status"] in {"pending", "published", "claimed"}
        ]

    def test_early_reply_owns_wait_and_recovery_uses_current_authority(self):
        self.respond()
        self.wait()
        self.assertEqual([x["reason"] for x in self.active_owner()], ["request_reply"])
        self.handle()
        self.age_reply()
        recovered = continuity.reconcile(self.root)["recovery"]["queued"]
        self.assertEqual(len(recovered), 1)
        packet = continuity.entry_packet(self.root, activation_id=recovered[0])
        self.assertEqual(packet["activation"]["control_epoch"], 2)
        self.assertEqual(
            packet["recovery_incidents"][0]["cause"], "unconfirmed_owner_checkpoint"
        )
        self.assertTrue(
            continuity.claim(
                self.root,
                activation_id=recovered[0],
                actor_id="owner",
                expected_epoch=2,
            )["actionable"]
        )

    def test_late_reply_retains_owner_recovery_until_next_checkpoint(self):
        self.wait()
        self.respond()
        self.handle()
        self.age_reply()
        self.assertEqual(len(continuity.reconcile(self.root)["recovery"]["queued"]), 1)
        self.wait(["check:NEXT"], handled_results=[self.outcome])
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])

    def test_healthy_reply_route_has_no_unrepairable_missing_activation_diagnostic(
        self,
    ):
        self.wait()
        self.respond()
        for repair in (False, True, False):
            result = continuity.self_check(self.root, repair=repair)
            self.assertNotIn(
                "satisfied_wait_not_activated", [x["code"] for x in result["findings"]]
            )
        self.handle()
        findings = continuity.self_check(self.root)["findings"]
        claimed = [x for x in findings if x["code"] == "claimed_result_unhandled"]
        self.assertEqual(claimed[0]["outcome_ids"], [self.outcome])

    def test_independent_claimed_reply_can_be_handled_at_next_wait(self):
        self.wait(["check:A"])
        self.respond()
        self.handle()
        self.age_reply()
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])
        updated = self.wait(["check:B"], handled_results=[self.outcome])
        self.assertIsNone(updated["activation_id"])
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM handled_results "
                    "WHERE work_id='WORK-1' AND outcome_id=?",
                    (self.outcome,),
                ).fetchone()
            )

    def test_cross_work_acknowledgement_is_rejected(self):
        self.wait()
        self.respond()
        self.handle()
        continuity.start_work(
            self.root, work_id="OTHER", owner_actor="owner", objective="Other"
        )
        with self.assertRaises(continuity.ContinuityError):
            continuity.checkpoint(
                self.root,
                work_id="OTHER",
                actor_id="owner",
                expected_revision=1,
                mode="waiting",
                summary="Other",
                wait_on=["check:OTHER"],
                handled_results=[self.outcome],
            )

    def test_explicit_stop_resume_preserves_one_owner_recovery(self):
        self.wait()
        self.respond()
        self.handle()
        self.age_reply()
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            actor_id="owner",
            state="stopped",
            reason="Stop",
        )
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            actor_id="owner",
            state="armed",
            reason="Resume",
        )
        self.assertEqual(len(continuity.reconcile(self.root)["recovery"]["queued"]), 1)
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])

    def test_explicit_replay_uses_a_new_product_route(self):
        self.wait()
        self.respond()
        self.handle()
        self.checkpoint(
            mode="paused", summary="Handled", handled_results=[self.outcome]
        )
        replay = self.wait(reprocess_results=[self.outcome])
        self.assertIsNotNone(replay["activation_id"])
        packet = continuity.entry_packet(
            self.root, activation_id=replay["activation_id"]
        )
        self.assertEqual(packet["activation"]["reason"], "wait_satisfied")

    def test_unclaimed_reply_does_not_authorize_acknowledgement(self):
        self.wait(["check:A"])
        self.respond()
        with self.assertRaises(continuity.ContinuityError):
            self.wait(["check:B"], handled_results=[self.outcome])

    def test_pause_and_complete_end_owner_checkpoint_recovery(self):
        self.wait()
        self.respond()
        self.handle()
        self.age_reply()
        self.checkpoint(mode="paused", summary="Paused", handled_results=[self.outcome])
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])
        self.checkpoint(mode="complete", summary="Complete")
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])

    def independent_check(self, mode):
        sources = ["obligation:" + self.request["obligation_id"], "check:INDEPENDENT"]
        self.wait(sources, wait_mode=mode)
        self.respond()
        self.handle()
        self.wait(sources, wait_mode=mode, handled_results=[self.outcome])
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "CHECK-FINISHED",
                "source_kind": "local_check",
                "operation_id": "INDEPENDENT",
                "terminal_status": "completed",
            },
        )
        activation = next(
            x
            for x in self.active_owner()
            if x["reason"] == "wait_satisfied" and x["work_revision"] == 3
        )
        self.assertEqual(len(activation["manifest"]), 1)
        self.assertNotIn(self.outcome, activation["manifest"])

    def test_all_wait_retains_independent_outcome_after_reply_handling(self):
        self.independent_check("all")

    def test_any_wait_retains_independent_outcome_after_reply_handling(self):
        self.independent_check("any")

    def test_other_return_actor_does_not_own_the_owners_wait(self):
        self.request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REVIEW-OTHER",
            idempotency_key="REVIEW-OTHER",
            sender_actor="owner",
            recipient_actor="reviewer",
            return_actor="other",
            message="Independent return",
            requires_reply=True,
            max_reminders=0,
        )
        self.wait()
        self.respond()
        self.assertEqual([x["reason"] for x in self.active_owner()], ["wait_satisfied"])
        continuity.claim(
            self.root,
            activation_id=self.reply["activation_id"],
            actor_id="other",
            expected_epoch=self.reply["control_epoch"],
        )
        continuity.request_handle(
            self.root,
            request_id=self.request["request_id"],
            actor_id="other",
            activation_id=self.reply["activation_id"],
        )
        self.age_reply()
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])

    def replay_fixture(self, mode):
        fixture = ReplyRouteTests("test_explicit_replay_uses_a_new_product_route")
        fixture.recovery_enabled = False
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.wait()
        fixture.respond()
        fixture.handle()
        fixture.checkpoint(
            mode="paused", summary="Handled", handled_results=[fixture.outcome]
        )
        continuity.observe_signal(
            fixture.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "EXTRA-FINISHED",
                "source_kind": "local_check",
                "operation_id": "EXTRA",
                "terminal_status": "completed",
            },
        )
        replay = fixture.wait(
            ["obligation:" + fixture.request["obligation_id"], "check:EXTRA"],
            wait_mode=mode,
            reprocess_results=[fixture.outcome],
        )
        with sqlite3.connect(continuity.database_path(fixture.root)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM recovery_policies").fetchone()[
                    0
                ],
                0,
            )
        return fixture, replay["activation_id"]

    def verify_restored_replay(self, fixture, old_activation):
        for _ in range(3):
            continuity.reconcile(fixture.root)
            findings = continuity.self_check(fixture.root, repair=True)["findings"]
            self.assertNotIn(
                "satisfied_wait_not_activated", [item["code"] for item in findings]
            )
        active = [x for x in fixture.active_owner() if x["reason"] == "wait_satisfied"]
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0]["activation_id"], old_activation)
        self.assertEqual(active[0]["work_revision"], 4)
        continuity.claim(
            fixture.root,
            activation_id=active[0]["activation_id"],
            actor_id="owner",
            expected_epoch=active[0]["control_epoch"],
        )
        fixture.checkpoint(
            mode="paused", summary="Replay inspected", handled_results=[fixture.outcome]
        )

    def replay_rearm(self, scope, mode):
        fixture, old_activation = self.replay_fixture(mode)
        for state in ("stopped", "armed"):
            continuity.set_recovery_control(
                fixture.root,
                scope_kind=scope,
                scope_id="WORK-1" if scope == "work" else "owner",
                state=state,
                actor_id="owner",
                reason="Explicit control",
            )
        self.verify_restored_replay(fixture, old_activation)

    def test_replay_work_rearm_all_without_recovery(self):
        self.replay_rearm("work", "all")

    def test_replay_work_rearm_any_without_recovery(self):
        self.replay_rearm("work", "any")

    def test_replay_actor_rearm_all_without_recovery(self):
        self.replay_rearm("actor", "all")

    def test_replay_actor_rearm_any_without_recovery(self):
        self.replay_rearm("actor", "any")

    def replay_rebind(self, mode):
        fixture, old_activation = self.replay_fixture(mode)
        continuity.register_actor(
            fixture.root,
            actor_id="owner",
            role="implementation",
            host="codex",
            target_thread_id="thread-owner-new",
            completion_delivery_mode="off",
        )
        self.verify_restored_replay(fixture, old_activation)

    def test_replay_endpoint_rebind_all_without_recovery(self):
        self.replay_rebind("all")

    def test_replay_endpoint_rebind_any_without_recovery(self):
        self.replay_rebind("any")

    def test_original_reply_claim_cannot_acknowledge_pending_replay(self):
        fixture, _ = self.replay_fixture("all")
        with self.assertRaises(continuity.ContinuityError):
            fixture.checkpoint(
                mode="paused",
                summary="Invalid stale acknowledgement",
                handled_results=[fixture.outcome],
            )
        self.assertEqual(fixture.state()["works"][0]["revision"], 4)


if __name__ == "__main__":
    unittest.main()
