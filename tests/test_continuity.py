from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import cli, continuity, core, local_checks, watcher


class ContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.register("sol", "implementation", "thread-sol")
        self.register("astra", "review", "thread-astra")

    def register(self, actor: str, role: str, thread: str) -> dict:
        return continuity.register_actor(
            self.root,
            actor_id=actor,
            role=role,
            host="codex",
            target_thread_id=thread,
            completion_delivery_mode="off",
        )

    def start(self, work_id: str = "WORK-1") -> None:
        continuity.start_work(
            self.root,
            work_id=work_id,
            owner_actor="sol",
            objective="Ship the accepted package",
        )

    def finish_check(self, check_id: str) -> None:
        directory = local_checks.check_dir(
            self.root, check_id, state_dir=core.DEFAULT_STATE_DIR
        )
        directory.mkdir(parents=True, exist_ok=True)
        core.atomic_json(
            directory / "check.json",
            {
                "schema_version": 1,
                "kind": local_checks.CHECK_KIND,
                "check_id": check_id,
                "status": "passed",
            },
        )
        core.atomic_json(
            directory / "verification-result.json",
            {
                "schema_version": 1,
                "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
                "check_id": check_id,
                "status": "passed",
            },
        )
        continuity.reconcile(self.root)

    def test_review_obligation_resumes_requester_without_revoking_reviewer(
        self,
    ) -> None:
        self.start()
        assignment = continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="REVIEW-1",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="Review the combined diff",
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Waiting for review",
            wait_on=["obligation:REVIEW-1"],
        )

        before = continuity.status(self.root, work_id="WORK-1")
        reviewer = next(
            item
            for item in before["activations"]
            if item["activation_id"] == assignment["activation_id"]
        )
        self.assertEqual(reviewer["status"], "published")
        continuity.claim(
            self.root,
            activation_id=assignment["activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )

        continuity.resolve_obligation(
            self.root,
            obligation_id="REVIEW-1",
            actor_id="astra",
            activation_id=assignment["activation_id"],
            status_value="completed",
            result_ref="artifact://review/accepted",
        )
        after = continuity.status(self.root, work_id="WORK-1")
        owner = next(
            item
            for item in after["activations"]
            if item["actor_id"] == "sol" and item["reason"] == "wait_satisfied"
        )
        self.assertEqual(owner["wake_target"]["target_thread_id"], "thread-sol")
        packet = continuity.entry_packet(
            self.root, activation_id=owner["activation_id"]
        )
        self.assertEqual(packet["work"]["revision"], 2)
        self.assertEqual(packet["obligations"][0]["status"], "completed")

    def test_idempotent_resolution_retries_pending_publication(self) -> None:
        self.start()
        assignment = continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="REVIEW-1",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="Review",
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_on=["obligation:REVIEW-1"],
        )
        continuity.claim(
            self.root,
            activation_id=assignment["activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        with (
            mock.patch.object(
                core, "write_followup_event", side_effect=OSError("delivery denied")
            ),
            self.assertRaisesRegex(continuity.ContinuityError, "state was committed"),
        ):
            continuity.resolve_obligation(
                self.root,
                obligation_id="REVIEW-1",
                actor_id="astra",
                activation_id=assignment["activation_id"],
                status_value="completed",
                result_ref="artifact://review/accepted",
            )
        pending = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["reason"] == "wait_satisfied"
        )
        self.assertEqual(pending["status"], "pending")

        repeated = continuity.resolve_obligation(
            self.root,
            obligation_id="REVIEW-1",
            actor_id="astra",
            activation_id=assignment["activation_id"],
            status_value="completed",
            result_ref="artifact://review/accepted",
        )
        self.assertTrue(repeated["idempotent"])
        published = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["reason"] == "wait_satisfied"
        )
        self.assertEqual(published["status"], "published")

    def test_completed_work_rejects_open_required_obligation(self) -> None:
        self.start()
        continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="REVIEW-1",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="Required review",
        )
        with self.assertRaisesRegex(continuity.ContinuityError, "remain open"):
            continuity.checkpoint(
                self.root,
                work_id="WORK-1",
                actor_id="sol",
                expected_revision=1,
                mode="complete",
                summary="Done",
            )

    def test_stale_revision_and_completed_work_are_immutable(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="complete",
            summary="Done",
        )
        with self.assertRaisesRegex(continuity.ContinuityError, "stale work revision"):
            continuity.checkpoint(
                self.root,
                work_id="WORK-1",
                actor_id="sol",
                expected_revision=1,
                mode="paused",
                summary="Old writer",
            )
        with self.assertRaisesRegex(continuity.ContinuityError, "immutable"):
            continuity.checkpoint(
                self.root,
                work_id="WORK-1",
                actor_id="sol",
                expected_revision=2,
                mode="paused",
                summary="Reopen",
            )

    def test_any_wait_handles_one_result_then_allows_a_later_result(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait for either check",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
        )
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-a",
                "source_kind": "local_check",
                "operation_id": "A",
                "terminal_status": "completed",
            },
        )
        first = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["actor_id"] == "sol" and item["status"] == "published"
        )
        continuity.claim(
            self.root,
            activation_id=first["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="waiting",
            summary="A handled; still waiting for B",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
            handled_results=first["manifest"],
        )
        self.assertEqual(continuity.reconcile(self.root)["published"], [])

        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-b",
                "source_kind": "local_check",
                "operation_id": "B",
                "terminal_status": "completed",
            },
        )
        active = [
            item
            for item in continuity.status(self.root)["activations"]
            if item["actor_id"] == "sol" and item["status"] == "published"
        ]
        self.assertEqual(len(active), 1)
        result = next(
            item
            for item in continuity.status(self.root)["managed_results"]
            if item["outcome_id"] == active[0]["manifest"][0]
        )
        self.assertEqual(result["source_key"], "check:B")

    def test_claimed_wait_result_is_not_republished_by_reconcile(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
        )
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-a",
                "source_kind": "local_check",
                "operation_id": "A",
                "terminal_status": "completed",
            },
        )
        activation = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["reason"] == "wait_satisfied"
        )
        continuity.claim(
            self.root,
            activation_id=activation["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )

        continuity.reconcile(self.root)
        continuity.self_check(self.root, repair=True)
        wait_activations = [
            item
            for item in continuity.status(self.root)["activations"]
            if item["reason"] == "wait_satisfied"
        ]

        self.assertEqual(len(wait_activations), 1)
        self.assertEqual(wait_activations[0]["status"], "claimed")

    def test_owner_rebind_reissues_claimed_wait_result(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_on=["check:A"],
        )
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-a",
                "source_kind": "local_check",
                "operation_id": "A",
                "terminal_status": "completed",
            },
        )
        old = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["reason"] == "wait_satisfied"
        )
        continuity.claim(
            self.root,
            activation_id=old["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )

        self.register("sol", "implementation", "thread-sol-2")
        report = continuity.status(self.root)
        current = [
            item
            for item in report["activations"]
            if item["reason"] == "wait_satisfied"
        ]
        replacement = next(item for item in current if item["status"] == "published")

        self.assertEqual(len(current), 2)
        self.assertEqual(replacement["endpoint_generation"], 2)
        self.assertEqual(
            replacement["wake_target"]["target_thread_id"], "thread-sol-2"
        )
        continuity.claim(
            self.root,
            activation_id=replacement["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        self.assertNotIn(
            "satisfied_wait_not_activated",
            {item["code"] for item in continuity.self_check(self.root)["findings"]},
        )

    def test_all_wait_counts_acknowledged_source_but_delivers_only_new_result(
        self,
    ) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait for either result first",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
        )
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-a",
                "source_kind": "local_check",
                "operation_id": "A",
                "terminal_status": "completed",
            },
        )
        first = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["reason"] == "wait_satisfied"
        )
        continuity.claim(
            self.root,
            activation_id=first["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="waiting",
            summary="A handled; require both sources",
            wait_mode="all",
            wait_on=["check:A", "check:B"],
            handled_results=first["manifest"],
        )

        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-b",
                "source_kind": "local_check",
                "operation_id": "B",
                "terminal_status": "completed",
            },
        )
        current = continuity.status(self.root)
        activation = next(
            item
            for item in current["activations"]
            if item["status"] == "published" and item["reason"] == "wait_satisfied"
        )
        delivered = next(
            item
            for item in current["managed_results"]
            if item["outcome_id"] == activation["manifest"][0]
        )

        self.assertEqual(len(activation["manifest"]), 1)
        self.assertEqual(delivered["source_key"], "check:B")

    def test_handled_result_survives_pause_and_new_wait(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
        )
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-a",
                "source_kind": "local_check",
                "operation_id": "A",
                "terminal_status": "completed",
            },
        )
        first = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["reason"] == "wait_satisfied"
        )
        continuity.claim(
            self.root,
            activation_id=first["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="paused",
            summary="Pause after A",
            handled_results=first["manifest"],
        )
        resumed = continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=3,
            mode="waiting",
            summary="Resume wait",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
        )

        self.assertIsNone(resumed["activation_id"])
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-b",
                "source_kind": "local_check",
                "operation_id": "B",
                "terminal_status": "completed",
            },
        )
        active = [
            item
            for item in continuity.status(self.root)["activations"]
            if item["status"] == "published" and item["reason"] == "wait_satisfied"
        ]
        self.assertEqual(len(active), 1)

    def test_retained_result_evidence_enrichment_keeps_one_outcome(self) -> None:
        self.start()
        directory = local_checks.check_dir(
            self.root, "A", state_dir=core.DEFAULT_STATE_DIR
        )
        directory.mkdir(parents=True)
        core.atomic_json(
            directory / "check.json",
            {
                "schema_version": 1,
                "kind": local_checks.CHECK_KIND,
                "check_id": "A",
                "status": "running",
            },
        )
        core.atomic_json(
            directory / "verification-result.json",
            {
                "schema_version": 1,
                "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
                "check_id": "A",
                "status": "passed",
            },
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_on=["check:A"],
        )
        first = continuity.status(self.root)["managed_results"]
        core.atomic_json(
            directory / "evidence.json",
            {"schema_version": 1, "kind": "CHECK_EVIDENCE"},
        )
        core.atomic_json(
            directory / "check.json",
            {
                "schema_version": 1,
                "kind": local_checks.CHECK_KIND,
                "check_id": "A",
                "status": "passed",
            },
        )

        continuity.reconcile(self.root)
        current = continuity.status(self.root)["managed_results"]

        self.assertEqual(len(first), 1)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["outcome_id"], first[0]["outcome_id"])
        self.assertIn("evidence", current[0]["observed_facts"])

    def test_legacy_v2_result_identity_remains_acknowledged_after_upgrade(self) -> None:
        self.start()
        directory = local_checks.check_dir(
            self.root, "A", state_dir=core.DEFAULT_STATE_DIR
        )
        directory.mkdir(parents=True)
        core.atomic_json(
            directory / "check.json",
            {
                "schema_version": 1,
                "kind": local_checks.CHECK_KIND,
                "check_id": "A",
                "status": "passed",
            },
        )
        core.atomic_json(
            directory / "verification-result.json",
            {
                "schema_version": 1,
                "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
                "check_id": "A",
                "status": "passed",
            },
        )
        waiting = continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_on=["check:A"],
        )
        activation = next(
            item
            for item in waiting["activations"]
            if item["activation_id"] == waiting["activation_id"]
        )
        result = waiting["managed_results"][0]
        continuity.claim(
            self.root,
            activation_id=activation["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="paused",
            summary="Handled",
            handled_results=activation["manifest"],
        )
        legacy_digest = continuity._legacy_outcome_digest(
            result["source_key"], result["status"], result["observed_facts"]
        )
        legacy_id = f"result-{legacy_digest[:24]}"
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE results SET outcome_id=?, digest=? WHERE outcome_id=?",
                (legacy_id, legacy_digest, result["outcome_id"]),
            )
            connection.execute(
                "UPDATE handled_results SET outcome_id=? WHERE outcome_id=?",
                (legacy_id, result["outcome_id"]),
            )
            connection.execute(
                """UPDATE activations SET manifest_json=?
                   WHERE reason='wait_satisfied'""",
                (json.dumps([legacy_id]),),
            )
            connection.execute(
                """DELETE FROM metadata WHERE key IN (
                       'result_identity_version','handled_results_backfilled_v2'
                   )"""
            )

        continuity.initialize(self.root)
        resumed = continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=3,
            mode="waiting",
            summary="Wait again",
            wait_on=["check:A"],
        )
        current = continuity.status(self.root)

        self.assertIsNone(resumed["activation_id"])
        self.assertEqual(len(current["managed_results"]), 1)
        self.assertEqual(current["managed_results"][0]["outcome_id"], legacy_id)
        self.assertEqual(current["waits"][0]["handled_results"], [legacy_id])

    def test_legacy_duplicate_outcome_id_remains_an_acknowledgement_alias(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_on=["check:A"],
        )
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-a",
                "source_kind": "local_check",
                "operation_id": "A",
                "terminal_status": "completed",
            },
        )
        current = continuity.status(self.root)
        activation = next(
            item
            for item in current["activations"]
            if item["reason"] == "wait_satisfied"
        )
        result = current["managed_results"][0]
        alias_id = "result-legacy-published-id"
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                """INSERT INTO results(
                       outcome_id, source_key, status, event_id, digest,
                       data_json, recorded_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    alias_id,
                    result["source_key"],
                    result["status"],
                    result["event_id"],
                    "f" * 64,
                    json.dumps(result["observed_facts"], sort_keys=True),
                    "2026-09-12T23:59:59Z",
                ),
            )
            connection.execute(
                "UPDATE activations SET manifest_json=? WHERE activation_id=?",
                (json.dumps([alias_id]), activation["activation_id"]),
            )
            connection.execute(
                "DELETE FROM metadata WHERE key='result_identity_version'"
            )

        continuity.initialize(self.root)
        normalized = continuity.status(self.root)
        normalized_activation = next(
            item
            for item in normalized["activations"]
            if item["activation_id"] == activation["activation_id"]
        )
        continuity.claim(
            self.root,
            activation_id=activation["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="paused",
            summary="Handled through legacy identity",
            handled_results=[alias_id],
        )
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            alias_target = connection.execute(
                "SELECT outcome_id FROM result_aliases WHERE alias_outcome_id=?",
                (alias_id,),
            ).fetchone()[0]
            handled = connection.execute(
                "SELECT outcome_id FROM handled_results WHERE work_id='WORK-1'"
            ).fetchone()[0]

        self.assertEqual(len(normalized["managed_results"]), 1)
        self.assertEqual(normalized_activation["manifest"], [result["outcome_id"]])
        self.assertEqual(alias_target, result["outcome_id"])
        self.assertEqual(handled, result["outcome_id"])

    def test_transfer_fences_old_activation(self) -> None:
        self.start()
        result = continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="continue",
            summary="Continue",
            next_action="Implement slice two",
        )
        activation_id = result["activation_id"]
        with self.assertRaisesRegex(continuity.ContinuityError, "fencing"):
            continuity.transfer(
                self.root,
                work_id="WORK-1",
                from_actor="sol",
                to_actor="astra",
                expected_revision=2,
                reason="Explicit handoff",
                fenced=False,
            )
        transferred = continuity.transfer(
            self.root,
            work_id="WORK-1",
            from_actor="sol",
            to_actor="astra",
            expected_revision=2,
            reason="Isolated ownership handoff",
            fenced=True,
        )
        self.assertEqual(transferred["works"][0]["control_epoch"], 3)
        with self.assertRaisesRegex(continuity.ContinuityError, "stale|claimable"):
            continuity.claim(
                self.root,
                activation_id=activation_id,
                actor_id="sol",
                expected_epoch=1,
            )

    def test_endpoint_generation_reissues_open_assignment(self) -> None:
        self.start()
        first = continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="REVIEW-1",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="Review",
        )
        self.register("astra", "review", "thread-astra-2")
        current = continuity.status(self.root)["activations"]
        old = next(
            item for item in current if item["activation_id"] == first["activation_id"]
        )
        new = next(item for item in current if item["status"] == "published")
        self.assertEqual(old["status"], "revoked")
        self.assertEqual(new["endpoint_generation"], 2)
        self.assertEqual(new["wake_target"]["target_thread_id"], "thread-astra-2")

    def test_open_schedules_bounded_reminder_and_resolution_revokes_it(self) -> None:
        self.start()
        assigned = continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="REVIEW-1",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="Review",
            reminder_seconds=10,
            max_reminders=1,
        )
        after_open = continuity.status(self.root)
        reminder = next(
            item
            for item in after_open["activations"]
            if item["reason"] == "obligation_reminder"
        )
        self.assertEqual(reminder["status"], "published")
        self.assertIsNotNone(reminder["not_before"])
        self.assertEqual(after_open["obligations"][0]["reminder_count"], 1)

        continuity.claim(
            self.root,
            activation_id=assigned["activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        after_claim = continuity.status(self.root)
        reminder = next(
            item
            for item in after_claim["activations"]
            if item["reason"] == "obligation_reminder"
        )
        self.assertEqual(reminder["status"], "published")
        self.assertEqual(after_claim["obligations"][0]["reminder_count"], 1)

        continuity.resolve_obligation(
            self.root,
            obligation_id="REVIEW-1",
            actor_id="astra",
            activation_id=assigned["activation_id"],
            status_value="completed",
            result_ref="artifact://review/accepted",
        )
        after_resolution = continuity.status(self.root)
        reminder = next(
            item
            for item in after_resolution["activations"]
            if item["reason"] == "obligation_reminder"
        )
        self.assertEqual(reminder["status"], "revoked")
        assignment = next(
            item
            for item in after_resolution["activations"]
            if item["reason"] == "obligation_assigned"
        )
        self.assertEqual(assignment["status"], "claimed")
        with mock.patch.object(watcher, "unix_now", return_value=10**12):
            scan = watcher.scan_once([self.root], action="record")
        self.assertIn(
            "continuity_activation_revoked",
            {item["reason"] for item in scan["suppressed_signals"]},
        )

    def test_watcher_observes_typed_result_and_publishes_activation(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait for check",
            wait_on=["check:CHECK-1"],
        )
        result = self.root / "result.json"
        evidence = self.root / "evidence.json"
        result.write_text("{}", encoding="utf-8")
        evidence.write_text("{}", encoding="utf-8")
        core.write_followup_event(
            self.root,
            operation_id="CHECK-1",
            source_kind="local_check",
            terminal_status="completed",
            result_path=result,
            evidence_path=evidence,
            event_id="check-event",
        )

        watcher.scan_once(
            [self.root],
            action="record",
            record_handler=lambda *_args, **_kwargs: None,
        )
        active = [
            item
            for item in continuity.status(self.root)["activations"]
            if item["actor_id"] == "sol" and item["status"] == "published"
        ]
        self.assertEqual(len(active), 1)
        result = next(
            item
            for item in continuity.status(self.root)["managed_results"]
            if item["outcome_id"] == active[0]["manifest"][0]
        )
        self.assertEqual(result["source_key"], "check:CHECK-1")

    def test_self_check_repairs_satisfied_wait_without_activation(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_on=["check:CHECK-1"],
        )
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-check",
                "source_kind": "local_check",
                "operation_id": "CHECK-1",
                "terminal_status": "completed",
            },
        )
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute("DELETE FROM outbox")
            connection.execute("DELETE FROM activations")
        report = continuity.self_check(self.root, repair=True)
        self.assertEqual(report["finding_count"], 2)
        self.assertEqual(
            {item["code"] for item in report["findings"]},
            {"satisfied_wait_not_activated", "wait_source_missing"},
        )
        self.assertEqual(len(report["published"]), 1)

    def test_resolved_diagnostic_stays_dismissed_while_finding_is_unchanged(
        self,
    ) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_on=["check:MISSING-CHECK"],
        )

        first = continuity.self_check(self.root, repair=True)
        self.assertEqual(first["finding_count"], 1)
        diagnostic_id = continuity.status(self.root)["diagnostics"][0][
            "diagnostic_id"
        ]
        continuity.resolve_diagnostic(
            self.root,
            diagnostic_id=diagnostic_id,
            actor_id="sol",
            correction="Known synthetic missing source; no repair is required.",
        )

        repeated = continuity.self_check(self.root, repair=True)
        self.assertEqual(repeated["status"], "ok")
        self.assertEqual(repeated["finding_count"], 0)
        self.assertEqual(repeated["observed_finding_count"], 1)
        self.assertEqual(repeated["resolved_finding_count"], 1)
        self.assertEqual(continuity.status(self.root)["diagnostics"], [])
        read_only = continuity.self_check(self.root)
        self.assertEqual(read_only["status"], "ok")
        self.assertEqual(read_only["finding_count"], 0)
        self.assertEqual(read_only["resolved_finding_count"], 1)

    def test_actor_and_source_ids_cannot_escape_state_root(self) -> None:
        with self.assertRaises(continuity.ContinuityError):
            continuity.register_actor(
                self.root,
                actor_id="../outside",
                role="review",
                host="codex",
                target_thread_id="thread",
            )
        self.start()
        with self.assertRaises(continuity.ContinuityError):
            continuity.checkpoint(
                self.root,
                work_id="WORK-1",
                actor_id="sol",
                expected_revision=1,
                mode="waiting",
                summary="Wait",
                wait_on=["check:../outside"],
            )

    def test_entry_packet_is_bounded_and_prioritizes_manifest_obligation(self) -> None:
        self.start()
        latest: dict[str, str] = {}
        with mock.patch.object(
            continuity,
            "_repository_snapshot",
            return_value={"status": "unavailable", "reason": "test"},
        ):
            for index in range(continuity.MAX_ENTRY_OBLIGATIONS + 1):
                latest = continuity.open_obligation(
                    self.root,
                    work_id="WORK-1",
                    obligation_id=f"REVIEW-{index:03d}",
                    requester_actor="sol",
                    assignee_actor="astra",
                    resume_actor="sol",
                    summary="Review",
                    required=False,
                    max_reminders=0,
                )
            packet = continuity.entry_packet(
                self.root, activation_id=latest["activation_id"]
            )

        self.assertEqual(len(packet["obligations"]), continuity.MAX_ENTRY_OBLIGATIONS)
        self.assertTrue(packet["obligation_summary"]["truncated"])
        self.assertIn(
            f"REVIEW-{continuity.MAX_ENTRY_OBLIGATIONS:03d}",
            {item["obligation_id"] for item in packet["obligations"]},
        )

    def test_session_bound_host_is_rejected_for_exact_multi_chat_routing(self) -> None:
        with self.assertRaisesRegex(continuity.ContinuityError, "exact"):
            continuity.register_actor(
                self.root,
                actor_id="session-review",
                role="review",
                host="claude",
            )

    def test_require_ready_actor_registration_fails_before_actor_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(continuity.ContinuityError, "not ready"):
                continuity.register_actor(
                    root,
                    actor_id="review",
                    role="review",
                    host="codex",
                    target_thread_id="thread-review",
                    completion_delivery_mode="require-ready",
                )

            actors = continuity.actor_status(root)["actors"]

        self.assertEqual(actors, [])

    def test_cli_registers_actor_and_reads_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "--project-root",
                        str(root),
                        "continuity",
                        "actor-register",
                        "--actor-id",
                        "implementation",
                        "--role",
                        "implementation",
                        "--host",
                        "codex",
                        "--target-thread-id",
                        "thread-1",
                        "--completion-delivery-mode",
                        "off",
                    ]
                )
            report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["actors"][0]["actor_id"], "implementation")

    def test_cli_sends_managed_request_and_configures_recovery(self) -> None:
        self.start()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(
                [
                    "--project-root",
                    str(self.root),
                    "continuity",
                    "request-send",
                    "--work-id",
                    "WORK-1",
                    "--request-id",
                    "CLI-REQUEST",
                    "--idempotency-key",
                    "cli-request-send",
                    "--sender-actor",
                    "sol",
                    "--recipient-actor",
                    "astra",
                    "--return-actor",
                    "sol",
                    "--message",
                    "Review through the CLI.",
                    "--requires-reply",
                    "--max-reminders",
                    "0",
                ]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(report["requires_reply"])
        self.assertIsNotNone(report["obligation_id"])

    def test_claim_does_not_acknowledge_result_and_checkpoint_preserves_cursor(
        self,
    ) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait for either",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
        )
        first_result = self.root / "first-result.json"
        first_result.write_text('{"attempt":1}', encoding="utf-8")
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-a-1",
                "source_kind": "local_check",
                "operation_id": "A",
                "terminal_status": "completed",
                "result_path": str(first_result),
            },
        )
        first = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["reason"] == "wait_satisfied" and item["status"] == "published"
        )
        continuity.claim(
            self.root,
            activation_id=first["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )

        crash_recovery = continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="waiting",
            summary="Claim was not acknowledged",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
        )
        self.assertIsNotNone(crash_recovery["activation_id"])
        replay = next(
            item
            for item in crash_recovery["activations"]
            if item["activation_id"] == crash_recovery["activation_id"]
        )
        self.assertEqual(replay["manifest"], first["manifest"])
        continuity.claim(
            self.root,
            activation_id=replay["activation_id"],
            actor_id="sol",
            expected_epoch=3,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=3,
            mode="waiting",
            summary="A acknowledged; still waiting",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
            handled_results=replay["manifest"],
        )
        self.assertEqual(continuity.reconcile(self.root)["published"], [])

        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-b-1",
                "source_kind": "local_check",
                "operation_id": "B",
                "terminal_status": "completed",
            },
        )
        late_b = next(
            item
            for item in continuity.status(self.root)["activations"]
            if item["status"] == "published" and item["reason"] == "wait_satisfied"
        )
        late_b_result = next(
            item
            for item in continuity.status(self.root)["managed_results"]
            if item["outcome_id"] == late_b["manifest"][0]
        )
        self.assertEqual(late_b_result["source_key"], "check:B")

        second_result = self.root / "second-result.json"
        second_result.write_text('{"attempt":2}', encoding="utf-8")
        continuity.observe_signal(
            self.root,
            {
                "kind": "ORCHESTRATOR_FOLLOWUP_SIGNAL",
                "event_id": "event-a-2",
                "source_kind": "local_check",
                "operation_id": "A",
                "terminal_status": "completed",
                "result_path": str(second_result),
            },
        )
        result_ids = {
            item["outcome_id"]
            for item in continuity.status(self.root)["managed_results"]
            if item["source_key"] == "check:A"
        }
        self.assertEqual(len(result_ids), 2)

    def test_retained_terminal_check_is_reconciled_without_unseen_signal(self) -> None:
        config = self.root / ".orchestrator" / "checks.toml"
        config.write_text(
            textwrap.dedent(
                f"""
                [suites.gate]
                verification = "focused"
                [[suites.gate.commands]]
                label = "unit"
                argv = [{sys.executable!r}, "-c", "print('ok')"]
                """
            ),
            encoding="utf-8",
        )
        for check_id, wake_policy in (
            ("PREVIOUSLY-SEEN", "always"),
            ("NO-WAKE", "never"),
        ):
            local_checks.start_check(
                self.root,
                check_id=check_id,
                suite="gate",
                execution="foreground",
                wake_policy=wake_policy,
            )
            if wake_policy == "always":
                watcher.scan_once([self.root], action="record")
            work_id = f"WORK-{check_id}"
            self.start(work_id)
            result = continuity.checkpoint(
                self.root,
                work_id=work_id,
                actor_id="sol",
                expected_revision=1,
                mode="waiting",
                summary="Wait for retained result",
                wait_on=[f"check:{check_id}"],
            )
            activation = next(
                item
                for item in result["activations"]
                if item["work_id"] == work_id and item["reason"] == "wait_satisfied"
            )
            self.assertEqual(activation["status"], "published")
            self.assertEqual(result["source_observations"][0]["state"], "terminal")

    def test_pause_and_rebind_invalidate_old_claim_authority(self) -> None:
        self.start()
        continued = continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="continue",
            summary="Continue",
            next_action="Implement",
        )
        continuity.claim(
            self.root,
            activation_id=continued["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="paused",
            summary="Pause",
        )
        with self.assertRaisesRegex(continuity.ContinuityError, "stale"):
            continuity.claim(
                self.root,
                activation_id=continued["activation_id"],
                actor_id="sol",
                expected_epoch=2,
            )

        obligation = continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="REVIEW-REBIND",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="Review",
        )
        continuity.claim(
            self.root,
            activation_id=obligation["activation_id"],
            actor_id="astra",
            expected_epoch=3,
        )
        self.register("astra", "review", "thread-astra-new")
        with self.assertRaisesRegex(continuity.ContinuityError, "current claimed"):
            continuity.resolve_obligation(
                self.root,
                obligation_id="REVIEW-REBIND",
                actor_id="astra",
                activation_id=obligation["activation_id"],
                status_value="completed",
                result_ref="artifact://old-review",
            )

    def test_complete_work_rejects_every_public_work_mutation(self) -> None:
        self.start()
        optional = continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="OPTIONAL",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="Optional review",
            required=False,
        )
        continuity.claim(
            self.root,
            activation_id=optional["activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="complete",
            summary="Done",
        )
        operations = (
            lambda: continuity.open_obligation(
                self.root,
                work_id="WORK-1",
                obligation_id="LATE",
                requester_actor="sol",
                assignee_actor="astra",
                resume_actor="sol",
                summary="Late",
            ),
            lambda: continuity.transfer(
                self.root,
                work_id="WORK-1",
                from_actor="sol",
                to_actor="astra",
                expected_revision=2,
                reason="Late",
                fenced=True,
            ),
            lambda: continuity.claim(
                self.root,
                activation_id=optional["activation_id"],
                actor_id="astra",
                expected_epoch=1,
            ),
            lambda: continuity.resolve_obligation(
                self.root,
                obligation_id="OPTIONAL",
                actor_id="astra",
                activation_id=optional["activation_id"],
                status_value="completed",
                result_ref="artifact://late",
            ),
            lambda: continuity.update_note(
                self.root,
                work_id="WORK-1",
                actor_id="sol",
                expected_note_revision=0,
                text="Late note",
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaisesRegex(
                continuity.ContinuityError, "immutable"
            ):
                operation()

    def test_resolution_requires_current_assignment_claim(self) -> None:
        self.start()
        assignment = continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="REVIEW-CLAIM",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="Review",
        )
        with self.assertRaisesRegex(continuity.ContinuityError, "current claimed"):
            continuity.resolve_obligation(
                self.root,
                obligation_id="REVIEW-CLAIM",
                actor_id="astra",
                activation_id=assignment["activation_id"],
                status_value="completed",
                result_ref="artifact://unclaimed",
            )

    def test_obligation_history_is_paginated_without_lifetime_quota(self) -> None:
        self.start()
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            now = core.utc_now()
            for index in range(256):
                connection.execute(
                    """INSERT INTO obligations(
                           obligation_id, work_id, requester_actor, assignee_actor,
                           resume_actor, required, status, summary, result_ref,
                           result_digest, generation, created_at, claimed_at,
                           claimed_activation_id, claimed_generation,
                           reminder_seconds, reminder_count, max_reminders, resolved_at
                       ) VALUES(?,?,?,?,?,0,'completed',?,NULL,NULL,1,?,NULL,NULL,
                                NULL,600,0,0,?)""",
                    (
                        f"HISTORY-{index:03d}",
                        "WORK-1",
                        "sol",
                        "astra",
                        "sol",
                        "Historical",
                        now,
                        now,
                    ),
                )
        opened = continuity.open_obligation(
            self.root,
            work_id="WORK-1",
            obligation_id="REVIEW-NEW",
            requester_actor="sol",
            assignee_actor="astra",
            resume_actor="sol",
            summary="New review",
            max_reminders=0,
        )
        self.assertIsNotNone(opened["activation_id"])
        report = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(report["obligation_page"]["total_count"], 257)
        self.assertTrue(report["obligation_page"]["truncated"])
        self.assertIsNotNone(report["obligation_page"]["next_cursor"])

    def test_managed_result_history_is_paginated(self) -> None:
        self.start()
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait",
            wait_on=["check:A"],
        )
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            for index in range(3):
                digest = f"{index + 1:064x}"
                connection.execute(
                    "INSERT INTO results VALUES(?,?,?,?,?,?,?)",
                    (
                        f"result-{index}",
                        "check:A",
                        "completed",
                        None,
                        digest,
                        "{}",
                        core.utc_now(),
                    ),
                )

        first = continuity.status(self.root, work_id="WORK-1", result_limit=2)
        second = continuity.status(
            self.root,
            work_id="WORK-1",
            result_limit=2,
            result_cursor=first["result_page"]["next_cursor"],
        )

        self.assertEqual(first["result_page"]["total_count"], 3)
        self.assertTrue(first["result_page"]["truncated"])
        self.assertEqual(len(first["managed_results"]), 2)
        self.assertFalse(second["result_page"]["truncated"])
        self.assertEqual(len(second["managed_results"]), 1)

    def test_operational_note_has_independent_revision_and_preserves_intent(
        self,
    ) -> None:
        self.start()
        before = continuity.status(self.root, work_id="WORK-1")["works"][0]
        note = continuity.update_note(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_note_revision=0,
            text="Reviewer should inspect the retained evidence.",
            references=["docs/review.md"],
        )
        after = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(note["revision"], 1)
        self.assertEqual(after["works"][0]["revision"], before["revision"])
        self.assertEqual(after["works"][0]["mode"], before["mode"])
        self.assertEqual(after["operational_notes"][0]["text"], note["text"])

    def test_managed_request_reply_is_atomic_idempotent_and_explicitly_handled(
        self,
    ) -> None:
        self.start()
        sent = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REQUEST-1",
            idempotency_key="send-request-1",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review the retained candidate.",
            requires_reply=True,
            required=True,
            max_reminders=0,
        )
        repeated = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REQUEST-1",
            idempotency_key="send-request-1",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review the retained candidate.",
            requires_reply=True,
            required=True,
            max_reminders=0,
        )
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(sent["obligation_id"], repeated["obligation_id"])
        with self.assertRaisesRegex(continuity.ContinuityError, "conflicts"):
            continuity.request_send(
                self.root,
                work_id="WORK-1",
                request_id="REQUEST-1",
                idempotency_key="send-request-1",
                sender_actor="sol",
                recipient_actor="astra",
                return_actor="sol",
                message="Different content.",
                requires_reply=True,
                required=True,
                max_reminders=0,
            )
        continuity.claim(
            self.root,
            activation_id=sent["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        receipt = continuity.request_respond(
            self.root,
            request_id="REQUEST-1",
            response_id="RESPONSE-RECEIPT",
            actor_id="astra",
            activation_id=sent["request_activation_id"],
            kind="receipt",
            content="Received.",
        )
        self.assertEqual(receipt["kind"], "receipt")
        terminal = continuity.request_respond(
            self.root,
            request_id="REQUEST-1",
            response_id="RESPONSE-FINAL",
            actor_id="astra",
            activation_id=sent["request_activation_id"],
            kind="terminal",
            terminal_status="completed",
            content="Accepted with no findings.",
        )
        self.assertEqual(terminal["terminal_status"], "completed")
        current = continuity.status(self.root, work_id="WORK-1")
        request = current["requests"][0]
        self.assertEqual(request["status"], "reply_ready")
        self.assertEqual(current["obligations"][0]["status"], "completed")
        reply_activation = request["reply_activation_id"]
        continuity.claim(
            self.root,
            activation_id=reply_activation,
            actor_id="sol",
            expected_epoch=1,
        )
        handled = continuity.request_handle(
            self.root,
            request_id="REQUEST-1",
            actor_id="sol",
            activation_id=reply_activation,
        )
        self.assertEqual(handled["status"], "handled")
        self.assertFalse(handled["idempotent"])

    def test_fyi_request_creates_no_reply_debt(self) -> None:
        self.start()
        sent = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="FYI-1",
            idempotency_key="send-fyi-1",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="The gate passed.",
            max_reminders=0,
        )
        self.assertIsNone(sent["obligation_id"])
        continuity.claim(
            self.root,
            activation_id=sent["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        current = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(current["requests"][0]["status"], "closed_no_reply")
        self.assertEqual(current["obligations"], [])

    def test_request_publication_failure_keeps_atomic_state_for_reconcile(self) -> None:
        self.start()
        with (
            mock.patch.object(
                core,
                "write_followup_event",
                side_effect=OSError("transport unavailable"),
            ),
            self.assertRaisesRegex(continuity.ContinuityError, "state was committed"),
        ):
            continuity.request_send(
                self.root,
                work_id="WORK-1",
                request_id="REQUEST-CRASH",
                idempotency_key="send-request-crash",
                sender_actor="sol",
                recipient_actor="astra",
                return_actor="sol",
                message="Survive publication failure.",
                requires_reply=True,
                max_reminders=0,
            )
        retained = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(len(retained["requests"]), 1)
        self.assertEqual(len(retained["obligations"]), 1)
        repaired = continuity.reconcile(self.root)
        self.assertEqual(len(repaired["published"]), 1)

    def test_saved_terminal_reply_republishes_without_model_regeneration(self) -> None:
        self.start()
        sent = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REQUEST-REPLY-CRASH",
            idempotency_key="send-request-reply-crash",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Return a durable answer.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=sent["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        with (
            mock.patch.object(
                core,
                "write_followup_event",
                side_effect=OSError("return transport unavailable"),
            ),
            self.assertRaisesRegex(continuity.ContinuityError, "state was committed"),
        ):
            continuity.request_respond(
                self.root,
                request_id="REQUEST-REPLY-CRASH",
                response_id="RESPONSE-REPLY-CRASH",
                actor_id="astra",
                activation_id=sent["request_activation_id"],
                kind="terminal",
                terminal_status="completed",
                content="Saved exactly once.",
            )
        retained = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(retained["requests"][0]["status"], "reply_ready")
        self.assertEqual(len(retained["responses"]), 1)
        repaired = continuity.reconcile(self.root)
        self.assertEqual(len(repaired["published"]), 1)
        repeated = continuity.request_respond(
            self.root,
            request_id="REQUEST-REPLY-CRASH",
            response_id="RESPONSE-REPLY-CRASH",
            actor_id="astra",
            activation_id=sent["request_activation_id"],
            kind="terminal",
            terminal_status="completed",
            content="Saved exactly once.",
        )
        self.assertTrue(repeated["idempotent"])

    def test_request_and_saved_reply_reissue_after_endpoint_rebind(self) -> None:
        self.start()
        sent = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REQUEST-REBIND",
            idempotency_key="send-request-rebind",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review after endpoint changes.",
            requires_reply=True,
            max_reminders=0,
        )
        self.register("astra", "review", "thread-astra-v2")
        current = continuity.status(self.root, work_id="WORK-1")
        request = current["requests"][0]
        self.assertNotEqual(
            request["request_activation_id"], sent["request_activation_id"]
        )
        replacement = request["request_activation_id"]
        continuity.claim(
            self.root,
            activation_id=replacement,
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.request_respond(
            self.root,
            request_id="REQUEST-REBIND",
            response_id="RESPONSE-REBIND",
            actor_id="astra",
            activation_id=replacement,
            kind="terminal",
            terminal_status="completed",
            content="Accepted.",
        )
        old_reply = continuity.status(self.root, work_id="WORK-1")["requests"][0][
            "reply_activation_id"
        ]
        self.register("sol", "implementation", "thread-sol-v2")
        rebound = continuity.status(self.root, work_id="WORK-1")
        new_reply = rebound["requests"][0]["reply_activation_id"]
        self.assertNotEqual(new_reply, old_reply)
        activation = next(
            item
            for item in rebound["activations"]
            if item["activation_id"] == new_reply
        )
        self.assertEqual(activation["wake_target"]["target_thread_id"], "thread-sol-v2")

    def test_rebind_preserves_assignment_wait_over_request_replay(self) -> None:
        self.start()
        sent = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REQUEST-WAIT-REBIND",
            idempotency_key="send-request-wait-rebind",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review after TEST-42.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=sent["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id=sent["obligation_id"],
            actor_id="astra",
            activation_id=sent["request_activation_id"],
            expected_revision=0,
            mode="waiting",
            summary="Waiting for TEST-42.",
            wait_on=["check:TEST-42"],
        )
        self.register("astra", "review", "thread-astra-v2")
        current = continuity.status(self.root, work_id="WORK-1")
        obligation = current["obligations"][0]
        assignment = current["assignment_checkpoints"][0]
        self.assertEqual(
            assignment["assignment_generation"], obligation["generation"]
        )
        active_request_replays = [
            item
            for item in current["activations"]
            if item["reason"] == "request_message"
            and item["status"] in {"pending", "published"}
        ]
        self.assertEqual(active_request_replays, [])

    def test_assignment_wait_and_recovery_are_scoped_and_deduplicated(self) -> None:
        continuity.configure_recovery(
            self.root,
            enabled=True,
            interval_seconds=10,
            max_interval_seconds=40,
        )
        self.start()
        first = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REVIEW-1",
            idempotency_key="send-review-1",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review one.",
            requires_reply=True,
            max_reminders=0,
        )
        second = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="QUESTION-2",
            idempotency_key="send-question-2",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Answer two.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Waiting for both replies.",
            wait_on=[
                f"obligation:{first['obligation_id']}",
                f"obligation:{second['obligation_id']}",
            ],
        )
        continuity.claim(
            self.root,
            activation_id=first["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        checkpointed = continuity.assignment_checkpoint(
            self.root,
            obligation_id=first["obligation_id"],
            actor_id="astra",
            activation_id=first["request_activation_id"],
            expected_revision=0,
            mode="waiting",
            summary="Waiting for the focused check.",
            wait_on=["check:TEST-42"],
        )
        self.assertEqual(checkpointed["mode"], "waiting")
        current = continuity.status(self.root, work_id="WORK-1")
        question = next(
            item
            for item in current["activations"]
            if item["activation_id"] == second["request_activation_id"]
        )
        self.assertEqual(question["status"], "published")

        continuity.claim(
            self.root,
            activation_id=second["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        old = "2000-01-01T00:00:00+00:00"
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE requests SET updated_at=? WHERE request_id='QUESTION-2'",
                (old,),
            )
            connection.execute(
                "UPDATE obligations SET claimed_at=? WHERE obligation_id=?",
                (old, second["obligation_id"]),
            )
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            state="stopped",
            actor_id="sol",
            reason="User input required.",
        )
        stopped = continuity.reconcile(self.root)
        self.assertEqual(stopped["recovery"]["queued"], [])
        self.assertIn("work:WORK-1", stopped["recovery"]["suppressed"][0]["reason"])
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            state="armed",
            actor_id="sol",
            reason="Continue autonomous inspection.",
        )
        recovered = continuity.reconcile(self.root)
        self.assertEqual(len(recovered["recovery"]["queued"]), 1)
        repeated = continuity.reconcile(self.root)
        self.assertEqual(repeated["recovery"]["queued"], [])
        recovery_activation = recovered["recovery"]["queued"][0]
        packet = continuity.entry_packet(
            self.root, activation_id=recovery_activation
        )
        self.assertEqual(packet["activation"]["reason"], "assignment_recovery")
        self.assertIn("unconfirmed", packet["recovery_incidents"][0]["cause"])
        continuity.claim(
            self.root,
            activation_id=recovery_activation,
            actor_id="astra",
            expected_epoch=packet["activation"]["control_epoch"],
        )
        after_claim = continuity.status(self.root, work_id="WORK-1")
        incident = after_claim["recovery"]["incidents"][0]
        self.assertEqual(incident["status"], "waiting")
        self.assertEqual(incident["attempts"], 1)
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE recovery_incidents SET next_inspection_at=? "
                "WHERE incident_id=?",
                (old, incident["incident_id"]),
            )
        next_recovery = continuity.reconcile(self.root)["recovery"]["queued"]
        self.assertEqual(len(next_recovery), 1)
        self.assertNotEqual(next_recovery[0], recovery_activation)

    def test_database_v2_upgrade_preserves_live_assignment_and_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for actor, role in (("sol", "implementation"), ("astra", "review")):
                continuity.register_actor(
                    root,
                    actor_id=actor,
                    role=role,
                    host="codex",
                    target_thread_id=f"thread-{actor}",
                    completion_delivery_mode="off",
                )
            continuity.start_work(
                root, work_id="LEGACY-V2", owner_actor="sol", objective="Preserve"
            )
            opened = continuity.open_obligation(
                root,
                work_id="LEGACY-V2",
                obligation_id="LEGACY-REVIEW",
                requester_actor="sol",
                assignee_actor="astra",
                resume_actor="sol",
                summary="Review",
                max_reminders=0,
            )
            path = continuity.database_path(root)
            with sqlite3.connect(path) as connection:
                for table in (
                    "recovery_incidents",
                    "recovery_controls",
                    "recovery_policies",
                    "assignment_checkpoints",
                    "responses",
                    "requests",
                ):
                    connection.execute(f"DROP TABLE {table}")
                connection.execute(
                    "UPDATE metadata SET value='2' WHERE key='schema_version'"
                )
            initialized = continuity.initialize(root)
            with sqlite3.connect(path) as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                obligation = connection.execute(
                    "SELECT status, generation FROM obligations "
                    "WHERE obligation_id='LEGACY-REVIEW'"
                ).fetchone()
                activation = connection.execute(
                    "SELECT status FROM activations WHERE activation_id=?",
                    (opened["activation_id"],),
                ).fetchone()
                outbox = connection.execute(
                    "SELECT status FROM outbox WHERE activation_id=?",
                    (opened["activation_id"],),
                ).fetchone()
                new_tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                recovery_policy_count = connection.execute(
                    "SELECT COUNT(*) FROM recovery_policies"
                ).fetchone()[0]
        self.assertEqual(initialized["database_schema_version"], 3)
        self.assertEqual(version, "3")
        self.assertEqual(obligation, ("open", 1))
        self.assertEqual(activation, ("published",))
        self.assertEqual(outbox, ("published",))
        self.assertIn("requests", new_tables)
        self.assertIn("recovery_incidents", new_tables)
        self.assertEqual(recovery_policy_count, 0)

    def test_future_database_schema_fails_before_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = continuity.database_path(root)
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO metadata VALUES('schema_version','99')"
                )
            with self.assertRaisesRegex(
                continuity.ContinuityError, "unsupported continuity database schema"
            ):
                continuity.initialize(root)
            with sqlite3.connect(path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
        self.assertEqual(tables, {"metadata"})

    def test_claimed_owner_continuation_recovers_until_next_checkpoint(self) -> None:
        continuity.configure_recovery(
            self.root,
            enabled=True,
            interval_seconds=10,
            max_interval_seconds=40,
        )
        self.start()
        continued = continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="continue",
            summary="Continue autonomously.",
            next_action="Implement the next accepted slice.",
            delay_seconds=0,
        )
        continuation_id = continued["activation_id"]
        continuity.claim(
            self.root,
            activation_id=continuation_id,
            actor_id="sol",
            expected_epoch=2,
        )
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE activations SET claimed_at=? WHERE activation_id=?",
                ("2000-01-01T00:00:00+00:00", continuation_id),
            )
        recovery = continuity.reconcile(self.root)
        self.assertEqual(len(recovery["recovery"]["queued"]), 1)
        recovery_id = recovery["recovery"]["queued"][0]
        packet = continuity.entry_packet(self.root, activation_id=recovery_id)
        self.assertEqual(packet["activation"]["reason"], "work_recovery")
        self.assertEqual(
            packet["recovery_incidents"][0]["cause"],
            "unconfirmed_owner_checkpoint",
        )
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            state="stopped",
            actor_id="sol",
            reason="Pause recovery before claim.",
        )
        with self.assertRaises(continuity.ContinuityError):
            continuity.claim(
                self.root,
                activation_id=recovery_id,
                actor_id="sol",
                expected_epoch=2,
            )
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            state="armed",
            actor_id="sol",
            reason="Resume recovery.",
        )
        replacement = continuity.reconcile(self.root)["recovery"]["queued"]
        self.assertEqual(len(replacement), 1)
        recovery_id = replacement[0]
        continuity.claim(
            self.root,
            activation_id=recovery_id,
            actor_id="sol",
            expected_epoch=2,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="paused",
            summary="A user decision is now required.",
        )
        current = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(current["recovery"]["incidents"][0]["status"], "resolved")

    def test_self_check_reports_local_assignment_dependency_cycle(self) -> None:
        self.start()
        assignments: dict[str, str] = {}
        for obligation_id in ("REVIEW-A", "REVIEW-B"):
            opened = continuity.open_obligation(
                self.root,
                work_id="WORK-1",
                obligation_id=obligation_id,
                requester_actor="sol",
                assignee_actor="astra",
                resume_actor="sol",
                summary=obligation_id,
                max_reminders=0,
            )
            assignments[obligation_id] = opened["activation_id"]
            continuity.claim(
                self.root,
                activation_id=opened["activation_id"],
                actor_id="astra",
                expected_epoch=1,
            )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id="REVIEW-A",
            actor_id="astra",
            activation_id=assignments["REVIEW-A"],
            expected_revision=0,
            mode="waiting",
            summary="A waits for B.",
            wait_on=["obligation:REVIEW-B"],
        )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id="REVIEW-B",
            actor_id="astra",
            activation_id=assignments["REVIEW-B"],
            expected_revision=0,
            mode="waiting",
            summary="B waits for A.",
            wait_on=["obligation:REVIEW-A"],
        )
        report = continuity.self_check(self.root)
        finding = next(
            item
            for item in report["findings"]
            if item["code"] == "assignment_dependency_cycle"
        )
        self.assertEqual(set(finding["obligation_ids"]), {"REVIEW-A", "REVIEW-B"})

    def test_recovery_capability_gap_is_visible_without_activation(self) -> None:
        continuity.configure_recovery(
            self.root,
            enabled=True,
            interval_seconds=10,
            max_interval_seconds=40,
        )
        self.start()
        continued = continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="continue",
            summary="Continue.",
            next_action="Next.",
            delay_seconds=0,
        )
        continuity.claim(
            self.root,
            activation_id=continued["activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE activations SET claimed_at=? WHERE activation_id=?",
                ("2000-01-01T00:00:00+00:00", continued["activation_id"]),
            )
            connection.execute(
                "UPDATE actors SET endpoint_json=? WHERE actor_id='sol'",
                ('{"schema_version":1,"kind":"ORCHESTRATOR_BINDING","host":"claude"}',),
            )
        first = continuity.reconcile(self.root)
        second = continuity.reconcile(self.root)
        self.assertEqual(first["recovery"]["queued"], [])
        self.assertEqual(second["recovery"]["queued"], [])
        self.assertIn(
            "capability_blocked",
            {item["reason"] for item in first["recovery"]["suppressed"]},
        )
        incident = continuity.status(self.root, work_id="WORK-1")["recovery"][
            "incidents"
        ][0]
        self.assertEqual(incident["status"], "capability_blocked")

    def test_database_v1_migrates_to_outcome_and_claim_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = continuity.database_path(root)
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO metadata VALUES('schema_version','1');
                    CREATE TABLE actors(
                        actor_id TEXT PRIMARY KEY,
                        role TEXT NOT NULL,
                        endpoint_json TEXT NOT NULL,
                        capabilities_json TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        active INTEGER NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE works(
                        work_id TEXT PRIMARY KEY,
                        objective TEXT NOT NULL,
                        references_json TEXT NOT NULL,
                        owner_actor TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        control_epoch INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        next_action TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE TABLE obligations(
                        obligation_id TEXT PRIMARY KEY,
                        work_id TEXT NOT NULL,
                        requester_actor TEXT NOT NULL,
                        assignee_actor TEXT NOT NULL,
                        resume_actor TEXT NOT NULL,
                        required INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        result_ref TEXT,
                        result_digest TEXT,
                        generation INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        claimed_at TEXT,
                        reminder_seconds REAL NOT NULL,
                        reminder_count INTEGER NOT NULL,
                        max_reminders INTEGER NOT NULL,
                        resolved_at TEXT
                    );
                    CREATE TABLE activations(
                        activation_id TEXT PRIMARY KEY,
                        work_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        work_revision INTEGER NOT NULL,
                        control_epoch INTEGER NOT NULL,
                        endpoint_generation INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        manifest_json TEXT NOT NULL,
                        wake_target_json TEXT NOT NULL,
                        not_before TEXT,
                        created_at TEXT NOT NULL,
                        claimed_at TEXT
                    );
                    CREATE TABLE results(
                        source_key TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        event_id TEXT,
                        digest TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    );
                    CREATE TABLE waits(
                        work_id TEXT PRIMARY KEY,
                        generation INTEGER NOT NULL,
                        mode TEXT NOT NULL,
                        sources_json TEXT NOT NULL,
                        handled_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO results VALUES(
                        'check:A','completed',NULL,
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                        '{}','2026-09-12T00:00:00Z'
                    );
                    INSERT INTO actors VALUES(
                        'legacy-sol','implementation','{}','[]',1,1,
                        '2026-09-12T00:00:00Z'
                    );
                    INSERT INTO actors VALUES(
                        'legacy-astra','review','{}','[]',1,1,
                        '2026-09-12T00:00:00Z'
                    );
                    INSERT INTO works VALUES(
                        'LEGACY-WORK','Legacy','[]','legacy-sol','waiting',1,1,
                        'Waiting',NULL,'2026-09-12T00:00:00Z',
                        '2026-09-12T00:00:00Z',NULL
                    );
                    INSERT INTO obligations VALUES(
                        'LEGACY-REVIEW','LEGACY-WORK','legacy-sol','legacy-astra',
                        'legacy-sol',1,'open','Review',NULL,NULL,3,
                        '2026-09-12T00:00:00Z','2026-09-12T00:01:00Z',
                        600,0,0,NULL
                    );
                    INSERT INTO activations VALUES(
                        'legacy-activation','LEGACY-WORK','legacy-astra',1,1,1,
                        'claimed','obligation_assigned',
                        '["obligation:LEGACY-REVIEW"]','{}',NULL,
                        '2026-09-12T00:00:00Z','2026-09-12T00:01:00Z'
                    );
                    INSERT INTO activations VALUES(
                        'legacy-wait-activation','LEGACY-WORK','legacy-sol',1,1,1,
                        'claimed','wait_satisfied','["check:A"]','{}',NULL,
                        '2026-09-12T00:00:00Z','2026-09-12T00:01:00Z'
                    );
                    INSERT INTO waits VALUES(
                        'LEGACY-WORK',1,'all','["check:A"]',
                        '["check:A"]',
                        '2026-09-12T00:02:00Z'
                    );
                    """
                )
            directory = local_checks.check_dir(
                root, "A", state_dir=core.DEFAULT_STATE_DIR
            )
            directory.mkdir(parents=True)
            core.atomic_json(
                directory / "check.json",
                {
                    "schema_version": 1,
                    "kind": local_checks.CHECK_KIND,
                    "check_id": "A",
                    "status": "passed",
                },
            )
            core.atomic_json(
                directory / "verification-result.json",
                {
                    "schema_version": 1,
                    "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
                    "check_id": "A",
                    "status": "passed",
                },
            )

            initialized = continuity.initialize(root)
            continuity.reconcile(root)
            with sqlite3.connect(path) as connection:
                obligation_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(obligations)"
                    )
                }
                activation_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(activations)"
                    )
                }
                result = connection.execute(
                    "SELECT outcome_id, source_key FROM results"
                ).fetchone()
                migrated_claim = connection.execute(
                    """SELECT claimed_activation_id, claimed_generation
                       FROM obligations WHERE obligation_id='LEGACY-REVIEW'"""
                ).fetchone()
                assignment_generation = connection.execute(
                    """SELECT assignment_generation FROM activations
                       WHERE activation_id='legacy-activation'"""
                ).fetchone()[0]
                migrated_handled = connection.execute(
                    """SELECT work_id, outcome_id, actor_id FROM handled_results
                       WHERE work_id='LEGACY-WORK'"""
                ).fetchone()
                migrated_manifest = connection.execute(
                    """SELECT manifest_json FROM activations
                       WHERE activation_id='legacy-wait-activation'"""
                ).fetchone()[0]
                result_count = connection.execute(
                    "SELECT COUNT(*) FROM results WHERE source_key='check:A'"
                ).fetchone()[0]

            continuity.resolve_obligation(
                root,
                obligation_id="LEGACY-REVIEW",
                actor_id="legacy-astra",
                activation_id="legacy-activation",
                status_value="completed",
                result_ref="artifact://legacy-review",
            )
            continuity.register_actor(
                root,
                actor_id="new-sol",
                role="implementation",
                host="codex",
                target_thread_id="thread-new-sol",
                completion_delivery_mode="off",
            )
            continuity.start_work(
                root,
                work_id="NEW-WORK",
                owner_actor="new-sol",
                objective="Continue after migration",
            )
            continued = continuity.checkpoint(
                root,
                work_id="NEW-WORK",
                actor_id="new-sol",
                expected_revision=1,
                mode="continue",
                summary="Continue",
                next_action="Next",
            )

        self.assertEqual(initialized["database_schema_version"], 3)
        self.assertIn("claimed_generation", obligation_columns)
        self.assertIn("assignment_generation", activation_columns)
        self.assertEqual(result, ("result-aaaaaaaaaaaaaaaaaaaaaaaa", "check:A"))
        self.assertEqual(migrated_claim, ("legacy-activation", 3))
        self.assertEqual(assignment_generation, 3)
        self.assertEqual(
            migrated_handled,
            ("LEGACY-WORK", "result-aaaaaaaaaaaaaaaaaaaaaaaa", "legacy-sol"),
        )
        self.assertEqual(migrated_manifest, '["result-aaaaaaaaaaaaaaaaaaaaaaaa"]')
        self.assertEqual(result_count, 1)
        self.assertIsNotNone(continued["activation_id"])

    def test_reply_recovery_uses_return_actor_and_stops_after_handling(self) -> None:
        self.register("requester", "coordination", "thread-requester")
        continuity.configure_recovery(
            self.root, enabled=True, interval_seconds=10, max_interval_seconds=40
        )
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="REVIEW-RETURN",
            idempotency_key="review-return",
            sender_actor="requester",
            recipient_actor="astra",
            return_actor="requester",
            message="Review independently.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Waiting for the recovered review.",
            wait_on=[f"obligation:{request['obligation_id']}"],
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.request_respond(
            self.root,
            request_id="REVIEW-RETURN",
            response_id="RESPONSE-RETURN",
            actor_id="astra",
            activation_id=request["request_activation_id"],
            kind="terminal",
            terminal_status="completed",
            content="Accepted.",
        )
        current = continuity.status(self.root, work_id="WORK-1")
        saved = current["requests"][0]
        reply_activation = saved["reply_activation_id"]
        reply_epoch = next(
            item["control_epoch"]
            for item in current["activations"]
            if item["activation_id"] == reply_activation
        )
        continuity.claim(
            self.root,
            activation_id=reply_activation,
            actor_id="requester",
            expected_epoch=reply_epoch,
        )
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE activations SET claimed_at=? WHERE activation_id=?",
                ("2000-01-01T00:00:00+00:00", reply_activation),
            )
        recovered = continuity.reconcile(self.root)["recovery"]["queued"]
        self.assertEqual(len(recovered), 1)
        packet = continuity.entry_packet(self.root, activation_id=recovered[0])
        self.assertEqual(packet["activation"]["actor_id"], "requester")
        continuity.claim(
            self.root,
            activation_id=recovered[0],
            actor_id="requester",
            expected_epoch=packet["activation"]["control_epoch"],
        )
        continuity.request_handle(
            self.root,
            request_id="REVIEW-RETURN",
            actor_id="requester",
            activation_id=reply_activation,
        )
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])
        incidents = continuity.status(self.root, work_id="WORK-1")["recovery"][
            "incidents"
        ]
        self.assertEqual(incidents[0]["status"], "resolved")

    def test_owner_checkpoint_and_complete_preserve_managed_communication(self) -> None:
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="OPTIONAL-REVIEW",
            idempotency_key="optional-review",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Optional review.",
            requires_reply=True,
            required=False,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.request_respond(
            self.root,
            request_id="OPTIONAL-REVIEW",
            response_id="OPTIONAL-RESPONSE",
            actor_id="astra",
            activation_id=request["request_activation_id"],
            kind="terminal",
            terminal_status="completed",
            content="No blocker.",
        )
        reply = continuity.status(self.root, work_id="WORK-1")["requests"][0]
        reply_activation = reply["reply_activation_id"]
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Waiting for an unrelated check.",
            wait_on=["check:OTHER"],
        )
        after_wait = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(
            next(
                item["status"]
                for item in after_wait["activations"]
                if item["activation_id"] == reply_activation
            ),
            "published",
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=2,
            mode="complete",
            summary="Product work complete; optional review remains routable.",
        )
        activation = next(
            item
            for item in continuity.status(self.root, work_id="WORK-1")[
                "activations"
            ]
            if item["activation_id"] == reply_activation
        )
        self.assertEqual(activation["status"], "published")
        continuity.claim(
            self.root,
            activation_id=reply_activation,
            actor_id="sol",
            expected_epoch=1,
        )
        continuity.request_handle(
            self.root,
            request_id="OPTIONAL-REVIEW",
            actor_id="sol",
            activation_id=reply_activation,
        )
        handled = continuity.status(self.root, work_id="WORK-1")["requests"][0]
        self.assertEqual(handled["status"], "handled")

    def test_independent_reply_recovery_incidents_resolve_independently(self) -> None:
        self.register("requester", "coordination", "thread-requester")
        continuity.configure_recovery(
            self.root, enabled=True, interval_seconds=10, max_interval_seconds=40
        )
        self.start()
        replies = []
        for index in (1, 2):
            request = continuity.request_send(
                self.root,
                work_id="WORK-1",
                request_id=f"REPLY-{index}",
                idempotency_key=f"reply-{index}",
                sender_actor="requester",
                recipient_actor="astra",
                return_actor="requester",
                message=f"Review {index}.",
                requires_reply=True,
                max_reminders=0,
            )
            continuity.claim(
                self.root,
                activation_id=request["request_activation_id"],
                actor_id="astra",
                expected_epoch=1,
            )
            continuity.request_respond(
                self.root,
                request_id=f"REPLY-{index}",
                response_id=f"RESPONSE-{index}",
                actor_id="astra",
                activation_id=request["request_activation_id"],
                kind="terminal",
                terminal_status="completed",
                content=f"Result {index}.",
            )
            saved = next(
                item
                for item in continuity.status(self.root, work_id="WORK-1")[
                    "requests"
                ]
                if item["request_id"] == f"REPLY-{index}"
            )
            continuity.claim(
                self.root,
                activation_id=saved["reply_activation_id"],
                actor_id="requester",
                expected_epoch=1,
            )
            replies.append(saved)
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE activations SET claimed_at=? "
                "WHERE activation_id IN (?,?)",
                (
                    "2000-01-01T00:00:00+00:00",
                    replies[0]["reply_activation_id"],
                    replies[1]["reply_activation_id"],
                ),
            )
        queued = continuity.reconcile(self.root)["recovery"]["queued"]
        self.assertEqual(len(queued), 2)
        continuity.request_handle(
            self.root,
            request_id="REPLY-1",
            actor_id="requester",
            activation_id=replies[0]["reply_activation_id"],
        )
        incidents = continuity.status(self.root, work_id="WORK-1")["recovery"][
            "incidents"
        ]
        self.assertEqual(
            {item["status"] for item in incidents}, {"queued", "resolved"}
        )

    def test_two_assignment_waits_receive_distinct_activations(self) -> None:
        self.start()
        requests = []
        for index in (1, 2):
            request = continuity.request_send(
                self.root,
                work_id="WORK-1",
                request_id=f"WAIT-{index}",
                idempotency_key=f"wait-{index}",
                sender_actor="sol",
                recipient_actor="astra",
                return_actor="sol",
                message=f"Review {index}.",
                requires_reply=True,
                max_reminders=0,
            )
            continuity.claim(
                self.root,
                activation_id=request["request_activation_id"],
                actor_id="astra",
                expected_epoch=1,
            )
            continuity.assignment_checkpoint(
                self.root,
                obligation_id=request["obligation_id"],
                actor_id="astra",
                activation_id=request["request_activation_id"],
                expected_revision=0,
                mode="waiting",
                summary=f"Wait for check {index}.",
                wait_on=[f"check:CHECK-{index}"],
            )
            requests.append(request)
        self.finish_check("CHECK-1")
        self.finish_check("CHECK-2")
        current = continuity.status(self.root, work_id="WORK-1")
        activations = [
            item
            for item in current["activations"]
            if item["reason"] == "assignment_wait_satisfied"
            and item["status"] == "published"
        ]
        self.assertEqual(len(activations), 2)
        manifests = [set(item["manifest"]) for item in activations]
        for request in requests:
            self.assertTrue(
                any(
                    f"obligation:{request['obligation_id']}" in manifest
                    for manifest in manifests
                )
            )

    def test_paused_assignment_resumes_with_original_claim(self) -> None:
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="PAUSE-1",
            idempotency_key="pause-1",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review after pause.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        paused = continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=0,
            mode="paused",
            summary="Awaiting an explicit local decision.",
        )
        self.assertIsNone(paused["activation_id"])
        resumed = continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=1,
            mode="continue",
            summary="Decision obtained.",
            next_action="Finish the review.",
            delay_seconds=0,
        )
        self.assertIsNotNone(resumed["activation_id"])

    def test_paused_assignment_rebind_requires_new_control_claim(self) -> None:
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="PAUSE-REBIND",
            idempotency_key="pause-rebind",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review after endpoint replacement.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=0,
            mode="paused",
            summary="Awaiting a decision.",
        )

        self.register("astra", "review", "thread-astra-new")
        current = continuity.status(self.root, work_id="WORK-1")
        control = next(
            item
            for item in current["activations"]
            if item["reason"] == "assignment_paused_control"
            and item["status"] == "published"
        )
        with self.assertRaisesRegex(continuity.ContinuityError, "current claimed"):
            continuity.assignment_checkpoint(
                self.root,
                obligation_id=request["obligation_id"],
                actor_id="astra",
                activation_id=request["request_activation_id"],
                expected_revision=1,
                mode="continue",
                summary="Old endpoint cannot resume.",
                next_action="Do not run.",
            )
        continuity.claim(
            self.root,
            activation_id=control["activation_id"],
            actor_id="astra",
            expected_epoch=control["control_epoch"],
        )
        resumed = continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=control["activation_id"],
            expected_revision=1,
            mode="continue",
            summary="Resume from the replacement endpoint.",
            next_action="Finish the review.",
            delay_seconds=0,
        )
        self.assertIsNotNone(resumed["activation_id"])

    def test_paused_request_can_be_cancelled_after_complete_and_rebind(self) -> None:
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="PAUSE-CANCEL",
            idempotency_key="pause-cancel",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Optional review that may be cancelled.",
            requires_reply=True,
            required=False,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=0,
            mode="paused",
            summary="Paused before product completion.",
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="complete",
            summary="Product work is complete.",
        )
        continuity.set_recovery_control(
            self.root,
            scope_kind="actor",
            scope_id="astra",
            state="stopped",
            actor_id="astra",
            reason="Replace the paused endpoint.",
        )
        self.register("astra", "review", "thread-astra-new")
        continuity.set_recovery_control(
            self.root,
            scope_kind="actor",
            scope_id="astra",
            state="armed",
            actor_id="astra",
            reason="Replacement endpoint is ready.",
        )
        current = continuity.status(self.root, work_id="WORK-1")
        control = next(
            item
            for item in current["activations"]
            if item["reason"] == "assignment_paused_control"
            and item["status"] == "published"
        )
        continuity.claim(
            self.root,
            activation_id=control["activation_id"],
            actor_id="astra",
            expected_epoch=control["control_epoch"],
        )
        continuity.request_respond(
            self.root,
            request_id="PAUSE-CANCEL",
            response_id="PAUSE-CANCELLED",
            actor_id="astra",
            activation_id=control["activation_id"],
            kind="terminal",
            terminal_status="cancelled",
            content="Review cancelled after product completion.",
        )
        self.assertEqual(
            continuity.status(self.root, work_id="WORK-1")["requests"][0][
                "status"
            ],
            "reply_ready",
        )

    def test_paused_assignment_stop_resume_keeps_current_claim(self) -> None:
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="PAUSE-STOP",
            idempotency_key="pause-stop",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Pause without replacing the endpoint.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=0,
            mode="paused",
            summary="Pause with a valid current claim.",
        )
        continuity.set_recovery_control(
            self.root,
            scope_kind="actor",
            scope_id="astra",
            state="stopped",
            actor_id="astra",
            reason="Temporary stop.",
        )
        continuity.set_recovery_control(
            self.root,
            scope_kind="actor",
            scope_id="astra",
            state="armed",
            actor_id="astra",
            reason="Resume without rebinding.",
        )
        current = continuity.status(self.root, work_id="WORK-1")
        self.assertFalse(
            any(
                item["reason"] == "assignment_paused_control"
                for item in current["activations"]
            )
        )
        resumed = continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=1,
            mode="continue",
            summary="Resume from the retained claim.",
            next_action="Finish the review.",
            delay_seconds=0,
        )
        self.assertIsNotNone(resumed["activation_id"])

    def test_claimed_satisfied_assignment_wait_is_recovered(self) -> None:
        continuity.configure_recovery(
            self.root, enabled=True, interval_seconds=10, max_interval_seconds=40
        )
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="WAIT-RECOVERY",
            idempotency_key="wait-recovery",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review after verification.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Waiting for the recovered review.",
            wait_on=[f"obligation:{request['obligation_id']}"],
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=0,
            mode="waiting",
            summary="Wait for verification.",
            wait_on=["check:VERIFY"],
        )
        self.finish_check("VERIFY")
        current = continuity.status(self.root, work_id="WORK-1")
        wake = next(
            item
            for item in current["activations"]
            if item["reason"] == "assignment_wait_satisfied"
        )
        continuity.claim(
            self.root,
            activation_id=wake["activation_id"],
            actor_id="astra",
            expected_epoch=wake["control_epoch"],
        )
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE obligations SET claimed_at=? WHERE obligation_id=?",
                ("2000-01-01T00:00:00+00:00", request["obligation_id"]),
            )
        recovered = continuity.reconcile(self.root)["recovery"]["queued"]
        self.assertEqual(len(recovered), 1)
        packet = continuity.entry_packet(self.root, activation_id=recovered[0])
        self.assertEqual(packet["activation"]["reason"], "assignment_recovery")

    def test_actor_and_project_stop_suppress_and_rearm_routes(self) -> None:
        self.register("sol", "implementation", "thread-sol")
        continuity.register_actor(
            self.root,
            actor_id="controller",
            role="coordination",
            host="codex",
            target_thread_id="thread-controller",
            capabilities=["project_control"],
            completion_delivery_mode="off",
        )
        self.start()
        continuity.set_recovery_control(
            self.root,
            scope_kind="actor",
            scope_id="astra",
            state="stopped",
            actor_id="astra",
            reason="Reviewer unavailable.",
        )
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="STOPPED-REQUEST",
            idempotency_key="stopped-request",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review when resumed.",
            requires_reply=True,
            max_reminders=0,
        )
        self.assertIsNone(request["request_activation_id"])
        resumed = continuity.set_recovery_control(
            self.root,
            scope_kind="actor",
            scope_id="astra",
            state="armed",
            actor_id="astra",
            reason="Reviewer available.",
        )
        self.assertEqual(len(resumed["published"]), 1)
        current = continuity.status(self.root, work_id="WORK-1")
        reissued = current["requests"][0]["request_activation_id"]
        continuity.set_recovery_control(
            self.root,
            scope_kind="project",
            scope_id="PROJECT",
            state="stopped",
            actor_id="controller",
            reason="Project stop.",
        )
        with self.assertRaisesRegex(continuity.ContinuityError, "stopped"):
            continuity.claim(
                self.root,
                activation_id=reissued,
                actor_id="astra",
                expected_epoch=1,
            )
        continuity.set_recovery_control(
            self.root,
            scope_kind="project",
            scope_id="PROJECT",
            state="armed",
            actor_id="controller",
            reason="Project resumed.",
        )
        replacement = continuity.status(self.root, work_id="WORK-1")["requests"][
            0
        ]["request_activation_id"]
        self.assertNotEqual(replacement, reissued)

    def test_claimed_request_rebind_during_stop_is_rearmed_once(self) -> None:
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="STOP-REBIND",
            idempotency_key="stop-rebind",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review across a stopped endpoint replacement.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.set_recovery_control(
            self.root,
            scope_kind="actor",
            scope_id="astra",
            state="stopped",
            actor_id="astra",
            reason="Replace the endpoint while stopped.",
        )
        self.register("astra", "review", "thread-astra-new")
        stopped = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(stopped["requests"][0]["status"], "claimed")
        resumed = continuity.set_recovery_control(
            self.root,
            scope_kind="actor",
            scope_id="astra",
            state="armed",
            actor_id="astra",
            reason="Replacement endpoint is ready.",
        )
        self.assertEqual(len(resumed["published"]), 1)
        current = continuity.status(self.root, work_id="WORK-1")
        replacement = current["requests"][0]["request_activation_id"]
        self.assertNotEqual(replacement, request["request_activation_id"])
        self.assertEqual(current["requests"][0]["status"], "delivery_pending")
        active = [
            item
            for item in current["activations"]
            if item["reason"] == "request_message"
            and item["status"] in {"pending", "published"}
        ]
        self.assertEqual([item["activation_id"] for item in active], [replacement])
        with self.assertRaisesRegex(continuity.ContinuityError, "endpoint generation"):
            continuity.claim(
                self.root,
                activation_id=request["request_activation_id"],
                actor_id="astra",
                expected_epoch=1,
            )
        continuity.reconcile(self.root)
        after_reconcile = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(
            len(
                [
                    item
                    for item in after_reconcile["activations"]
                    if item["reason"] == "request_message"
                    and item["status"] in {"pending", "published"}
                ]
            ),
            1,
        )

    def test_optional_waiting_request_finishes_after_work_completion(self) -> None:
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="COMPLETE-WAIT",
            idempotency_key="complete-wait",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Finish the optional review after verification.",
            requires_reply=True,
            required=False,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=0,
            mode="waiting",
            summary="Waiting for verification.",
            wait_on=["check:AFTER-COMPLETE"],
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="complete",
            summary="Product work is complete.",
        )
        self.finish_check("AFTER-COMPLETE")
        current = continuity.status(self.root, work_id="WORK-1")
        wake = next(
            item
            for item in current["activations"]
            if item["reason"] == "assignment_wait_satisfied"
            and item["status"] == "published"
        )
        continuity.claim(
            self.root,
            activation_id=wake["activation_id"],
            actor_id="astra",
            expected_epoch=wake["control_epoch"],
        )
        continuity.request_respond(
            self.root,
            request_id="COMPLETE-WAIT",
            response_id="COMPLETE-WAIT-RESPONSE",
            actor_id="astra",
            activation_id=wake["activation_id"],
            kind="terminal",
            terminal_status="completed",
            content="Review completed after the product work.",
        )
        reply = continuity.status(self.root, work_id="WORK-1")["requests"][0]
        continuity.claim(
            self.root,
            activation_id=reply["reply_activation_id"],
            actor_id="sol",
            expected_epoch=2,
        )
        continuity.request_handle(
            self.root,
            request_id="COMPLETE-WAIT",
            actor_id="sol",
            activation_id=reply["reply_activation_id"],
        )
        final = continuity.status(self.root, work_id="WORK-1")
        self.assertEqual(final["works"][0]["mode"], "complete")
        self.assertEqual(final["requests"][0]["status"], "handled")

    def test_unclaimed_and_continuing_requests_remain_reachable_after_complete(
        self,
    ) -> None:
        self.start()
        unclaimed = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="COMPLETE-UNCLAIMED",
            idempotency_key="complete-unclaimed",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Unclaimed optional review.",
            requires_reply=True,
            required=False,
            max_reminders=0,
        )
        continuing = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="COMPLETE-CONTINUE",
            idempotency_key="complete-continue",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Continuing optional review.",
            requires_reply=True,
            required=False,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=continuing["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continued = continuity.assignment_checkpoint(
            self.root,
            obligation_id=continuing["obligation_id"],
            actor_id="astra",
            activation_id=continuing["request_activation_id"],
            expected_revision=0,
            mode="continue",
            summary="Continue the review.",
            next_action="Return a terminal response.",
            delay_seconds=0,
        )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="complete",
            summary="Product work is complete.",
        )
        current = continuity.status(self.root, work_id="WORK-1")
        states = {
            item["activation_id"]: item["status"] for item in current["activations"]
        }
        self.assertEqual(states[unclaimed["request_activation_id"]], "published")
        self.assertEqual(states[continued["activation_id"]], "published")
        for request, activation_id, response_id in (
            (
                unclaimed,
                unclaimed["request_activation_id"],
                "COMPLETE-UNCLAIMED-RESPONSE",
            ),
            (continuing, continued["activation_id"], "COMPLETE-CONTINUE-RESPONSE"),
        ):
            continuity.claim(
                self.root,
                activation_id=activation_id,
                actor_id="astra",
                expected_epoch=1,
            )
            continuity.request_respond(
                self.root,
                request_id=request["request_id"],
                response_id=response_id,
                actor_id="astra",
                activation_id=activation_id,
                kind="terminal",
                terminal_status="completed",
                content="Optional review completed.",
            )
        self.assertEqual(
            {item["status"] for item in continuity.status(
                self.root, work_id="WORK-1"
            )["requests"]},
            {"reply_ready"},
        )

    def test_owner_checkpoint_preserves_reply_recovery_backoff(self) -> None:
        self.register("requester", "coordination", "thread-requester")
        continuity.configure_recovery(
            self.root, enabled=True, interval_seconds=10, max_interval_seconds=40
        )
        self.start()
        recoveries = []
        for index in (1, 2):
            request = continuity.request_send(
                self.root,
                work_id="WORK-1",
                request_id=f"BACKOFF-{index}",
                idempotency_key=f"backoff-{index}",
                sender_actor="requester",
                recipient_actor="astra",
                return_actor="requester",
                message=f"Review request {index}.",
                requires_reply=True,
                max_reminders=0,
            )
            continuity.claim(
                self.root,
                activation_id=request["request_activation_id"],
                actor_id="astra",
                expected_epoch=1,
            )
            continuity.request_respond(
                self.root,
                request_id=f"BACKOFF-{index}",
                response_id=f"BACKOFF-RESPONSE-{index}",
                actor_id="astra",
                activation_id=request["request_activation_id"],
                kind="terminal",
                terminal_status="completed",
                content=f"Review result {index}.",
            )
            saved = next(
                item
                for item in continuity.status(self.root, work_id="WORK-1")[
                    "requests"
                ]
                if item["request_id"] == f"BACKOFF-{index}"
            )
            continuity.claim(
                self.root,
                activation_id=saved["reply_activation_id"],
                actor_id="requester",
                expected_epoch=1,
            )
            with sqlite3.connect(continuity.database_path(self.root)) as connection:
                connection.execute(
                    "UPDATE activations SET claimed_at=? WHERE activation_id=?",
                    ("2000-01-01T00:00:00+00:00", saved["reply_activation_id"]),
                )
            recoveries.append(saved)
        queued = continuity.reconcile(self.root)["recovery"]["queued"]
        self.assertEqual(len(queued), 2)
        first_packet = continuity.entry_packet(self.root, activation_id=queued[0])
        continuity.claim(
            self.root,
            activation_id=queued[0],
            actor_id="requester",
            expected_epoch=first_packet["activation"]["control_epoch"],
        )
        before = continuity.status(self.root, work_id="WORK-1")["recovery"][
            "incidents"
        ]
        before_by_activation = {item["activation_id"]: item for item in before}

        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Wait for an unrelated local check.",
            wait_on=["check:UNRELATED"],
        )
        after = continuity.status(self.root, work_id="WORK-1")["recovery"][
            "incidents"
        ]
        after_by_activation = {item["activation_id"]: item for item in after}
        self.assertEqual(after_by_activation[queued[0]]["status"], "waiting")
        self.assertEqual(
            after_by_activation[queued[0]]["next_inspection_at"],
            before_by_activation[queued[0]]["next_inspection_at"],
        )
        self.assertEqual(after_by_activation[queued[1]]["status"], "queued")
        second_packet = continuity.entry_packet(self.root, activation_id=queued[1])
        continuity.claim(
            self.root,
            activation_id=queued[1],
            actor_id="requester",
            expected_epoch=second_packet["activation"]["control_epoch"],
        )
        self.assertEqual(continuity.reconcile(self.root)["recovery"]["queued"], [])

    def test_owner_rebind_during_each_stop_scope_rearms_one_current_route(
        self,
    ) -> None:
        for scope_kind in ("actor", "work", "project"):
            with (
                self.subTest(scope_kind=scope_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                continuity.register_actor(
                    root,
                    actor_id="sol",
                    role="implementation",
                    host="codex",
                    target_thread_id="thread-sol",
                    completion_delivery_mode="off",
                )
                continuity.register_actor(
                    root,
                    actor_id="controller",
                    role="coordination",
                    host="codex",
                    target_thread_id="thread-controller",
                    capabilities=["project_control"],
                    completion_delivery_mode="off",
                )
                continuity.start_work(
                    root,
                    work_id="OWNER-REBIND",
                    owner_actor="sol",
                    objective="Continue after endpoint replacement.",
                )
                continued = continuity.checkpoint(
                    root,
                    work_id="OWNER-REBIND",
                    actor_id="sol",
                    expected_revision=1,
                    mode="continue",
                    summary="Continue owner work.",
                    next_action="Complete the next slice.",
                    delay_seconds=0,
                )
                old_activation = continued["activation_id"]
                continuity.claim(
                    root,
                    activation_id=old_activation,
                    actor_id="sol",
                    expected_epoch=2,
                )
                scope_id = {
                    "actor": "sol",
                    "work": "OWNER-REBIND",
                    "project": "PROJECT",
                }[scope_kind]
                controller = "controller" if scope_kind == "project" else "sol"
                continuity.set_recovery_control(
                    root,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    state="stopped",
                    actor_id=controller,
                    reason="Replace the owner endpoint while stopped.",
                )
                continuity.register_actor(
                    root,
                    actor_id="sol",
                    role="implementation",
                    host="codex",
                    target_thread_id="thread-sol-new",
                    completion_delivery_mode="off",
                )
                continuity.set_recovery_control(
                    root,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    state="armed",
                    actor_id=controller,
                    reason="Replacement endpoint is ready.",
                )
                current = continuity.status(root, work_id="OWNER-REBIND")
                routes = [
                    item
                    for item in current["activations"]
                    if item["reason"] == "continue_checkpoint"
                    and item["status"] in {"pending", "published"}
                ]
                self.assertEqual(len(routes), 1)
                self.assertEqual(routes[0]["endpoint_generation"], 2)
                with self.assertRaisesRegex(
                    continuity.ContinuityError, "endpoint generation"
                ):
                    continuity.claim(
                        root,
                        activation_id=old_activation,
                        actor_id="sol",
                        expected_epoch=2,
                    )
                continuity.reconcile(root)
                continuity.set_recovery_control(
                    root,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    state="armed",
                    actor_id=controller,
                    reason="Idempotent re-arm.",
                )
                after = continuity.status(root, work_id="OWNER-REBIND")
                self.assertEqual(
                    len(
                        [
                            item
                            for item in after["activations"]
                            if item["reason"] == "continue_checkpoint"
                            and item["status"] in {"pending", "published"}
                            and item["endpoint_generation"] == 2
                        ]
                    ),
                    1,
                )

    def test_completed_non_request_wait_never_publishes_product_wakeup(self) -> None:
        for pathway in ("reconcile", "observe", "self-check"):
            with self.subTest(pathway=pathway), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for actor_id, role in (("sol", "implementation"), ("astra", "review")):
                    continuity.register_actor(
                        root,
                        actor_id=actor_id,
                        role=role,
                        host="codex",
                        target_thread_id=f"thread-{actor_id}",
                        completion_delivery_mode="off",
                    )
                continuity.start_work(
                    root,
                    work_id="COMPLETE-PRODUCT",
                    owner_actor="sol",
                    objective="Complete before an optional product assignment.",
                )
                assignment = continuity.open_obligation(
                    root,
                    work_id="COMPLETE-PRODUCT",
                    obligation_id="OPTIONAL-PRODUCT",
                    requester_actor="sol",
                    assignee_actor="astra",
                    resume_actor="sol",
                    summary="Optional product investigation.",
                    required=False,
                )
                continuity.claim(
                    root,
                    activation_id=assignment["activation_id"],
                    actor_id="astra",
                    expected_epoch=1,
                )
                continuity.assignment_checkpoint(
                    root,
                    obligation_id="OPTIONAL-PRODUCT",
                    actor_id="astra",
                    activation_id=assignment["activation_id"],
                    expected_revision=0,
                    mode="waiting",
                    summary="Wait for retained evidence.",
                    wait_on=["check:TERMINAL-PRODUCT"],
                )
                continuity.checkpoint(
                    root,
                    work_id="COMPLETE-PRODUCT",
                    actor_id="sol",
                    expected_revision=1,
                    mode="complete",
                    summary="Product work completed.",
                )
                directory = local_checks.check_dir(
                    root, "TERMINAL-PRODUCT", state_dir=core.DEFAULT_STATE_DIR
                )
                directory.mkdir(parents=True, exist_ok=True)
                core.atomic_json(
                    directory / "check.json",
                    {
                        "schema_version": 1,
                        "kind": local_checks.CHECK_KIND,
                        "check_id": "TERMINAL-PRODUCT",
                        "status": "passed",
                    },
                )
                core.atomic_json(
                    directory / "verification-result.json",
                    {
                        "schema_version": 1,
                        "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
                        "check_id": "TERMINAL-PRODUCT",
                        "status": "passed",
                    },
                )
                signal = {
                    "source_kind": "local_check",
                    "operation_id": "TERMINAL-PRODUCT",
                    "terminal_status": "completed",
                }
                if pathway == "reconcile":
                    continuity.reconcile(root)
                elif pathway == "observe":
                    continuity.observe_signal(root, signal)
                else:
                    continuity.set_recovery_control(
                        root,
                        scope_kind="actor",
                        scope_id="astra",
                        state="stopped",
                        actor_id="astra",
                        reason="Record evidence without delivery.",
                    )
                    continuity.observe_signal(root, signal)
                    with sqlite3.connect(continuity.database_path(root)) as connection:
                        connection.execute(
                            "UPDATE recovery_controls SET state='armed' "
                            "WHERE scope_kind='actor' AND scope_id='astra'"
                        )
                    continuity.self_check(root, repair=True)
                current = continuity.status(root, work_id="COMPLETE-PRODUCT")
                product_wakes = [
                    item
                    for item in current["activations"]
                    if item["reason"] == "assignment_wait_satisfied"
                    and item["status"] in {"pending", "published"}
                ]
                self.assertEqual(product_wakes, [])
                self.assertEqual(current["works"][0]["mode"], "complete")

    def test_assignment_handled_results_prevent_replay_without_opt_in(self) -> None:
        self.start()
        request = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="ACK-1",
            idempotency_key="ack-1",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Review after either check.",
            requires_reply=True,
            max_reminders=0,
        )
        continuity.claim(
            self.root,
            activation_id=request["request_activation_id"],
            actor_id="astra",
            expected_epoch=1,
        )
        continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=request["request_activation_id"],
            expected_revision=0,
            mode="waiting",
            summary="Wait for either check.",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
        )
        self.finish_check("A")
        current = continuity.status(self.root, work_id="WORK-1")
        first_wake = next(
            item
            for item in current["activations"]
            if item["reason"] == "assignment_wait_satisfied"
            and item["status"] == "published"
        )
        outcome = next(
            value for value in first_wake["manifest"] if value.startswith("result-")
        )
        continuity.claim(
            self.root,
            activation_id=first_wake["activation_id"],
            actor_id="astra",
            expected_epoch=first_wake["control_epoch"],
        )
        checkpointed = continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=first_wake["activation_id"],
            expected_revision=1,
            mode="waiting",
            summary="A handled; wait for B.",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
            handled_results=[outcome],
        )
        self.assertIsNone(checkpointed["activation_id"])
        self.finish_check("B")
        active = [
            item
            for item in continuity.status(self.root, work_id="WORK-1")[
                "activations"
            ]
            if item["reason"] == "assignment_wait_satisfied"
            and item["status"] == "published"
        ]
        self.assertEqual(len(active), 1)
        self.assertNotIn(outcome, active[0]["manifest"])
        second_outcome = next(
            value for value in active[0]["manifest"] if value.startswith("result-")
        )
        continuity.claim(
            self.root,
            activation_id=active[0]["activation_id"],
            actor_id="astra",
            expected_epoch=active[0]["control_epoch"],
        )
        replayed = continuity.assignment_checkpoint(
            self.root,
            obligation_id=request["obligation_id"],
            actor_id="astra",
            activation_id=active[0]["activation_id"],
            expected_revision=2,
            mode="waiting",
            summary="Replay A explicitly.",
            wait_mode="any",
            wait_on=["check:A", "check:B"],
            handled_results=[second_outcome],
            reprocess_results=[outcome],
        )
        replayed_status = continuity.status(self.root, work_id="WORK-1")
        replay = next(
            item
            for item in replayed_status["activations"]
            if item["activation_id"] == replayed["activation_id"]
        )
        self.assertIn(outcome, replay["manifest"])

    def test_work_stop_does_not_suppress_another_work(self) -> None:
        self.start("WORK-1")
        self.start("WORK-2")
        continuity.set_recovery_control(
            self.root,
            scope_kind="work",
            scope_id="WORK-1",
            state="stopped",
            actor_id="sol",
            reason="Only the first work needs user input.",
        )
        first = continuity.request_send(
            self.root,
            work_id="WORK-1",
            request_id="WORK-1-REQUEST",
            idempotency_key="work-1-request",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Held request.",
            requires_reply=True,
            max_reminders=0,
        )
        second = continuity.request_send(
            self.root,
            work_id="WORK-2",
            request_id="WORK-2-REQUEST",
            idempotency_key="work-2-request",
            sender_actor="sol",
            recipient_actor="astra",
            return_actor="sol",
            message="Independent request.",
            requires_reply=True,
            max_reminders=0,
        )
        self.assertIsNone(first["request_activation_id"])
        self.assertIsNotNone(second["request_activation_id"])

    def test_invalid_recovery_incident_does_not_block_healthy_peer(self) -> None:
        continuity.configure_recovery(
            self.root, enabled=True, interval_seconds=10, max_interval_seconds=40
        )
        self.start()
        obligation_ids = []
        for index in (1, 2):
            request = continuity.request_send(
                self.root,
                work_id="WORK-1",
                request_id=f"RECOVER-{index}",
                idempotency_key=f"recover-{index}",
                sender_actor="sol",
                recipient_actor="astra",
                return_actor="sol",
                message=f"Review {index}.",
                requires_reply=True,
                max_reminders=0,
            )
            obligation_ids.append(request["obligation_id"])
            continuity.claim(
                self.root,
                activation_id=request["request_activation_id"],
                actor_id="astra",
                expected_epoch=1,
            )
        continuity.checkpoint(
            self.root,
            work_id="WORK-1",
            actor_id="sol",
            expected_revision=1,
            mode="waiting",
            summary="Waiting for both recovered reviews.",
            wait_on=[f"obligation:{value}" for value in obligation_ids],
        )
        old = "2000-01-01T00:00:00+00:00"
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute("UPDATE obligations SET claimed_at=?", (old,))
            connection.execute("UPDATE requests SET updated_at=?", (old,))
        first = continuity.reconcile(self.root)["recovery"]["queued"]
        self.assertEqual(len(first), 2)
        for activation_id in first:
            packet = continuity.entry_packet(self.root, activation_id=activation_id)
            continuity.claim(
                self.root,
                activation_id=activation_id,
                actor_id="astra",
                expected_epoch=packet["activation"]["control_epoch"],
            )
        incidents = continuity.status(self.root, work_id="WORK-1")["recovery"][
            "incidents"
        ]
        with sqlite3.connect(continuity.database_path(self.root)) as connection:
            connection.execute(
                "UPDATE recovery_incidents SET next_inspection_at='invalid' "
                "WHERE incident_id=?",
                (incidents[0]["incident_id"],),
            )
            connection.execute(
                "UPDATE recovery_incidents SET next_inspection_at=? "
                "WHERE incident_id=?",
                (old, incidents[1]["incident_id"]),
            )
        reconciled = continuity.reconcile(self.root)["recovery"]
        self.assertEqual(len(reconciled["queued"]), 1)
        self.assertIn(
            "invalid_incident_timestamp",
            {item["reason"] for item in reconciled["suppressed"]},
        )


if __name__ == "__main__":
    unittest.main()
