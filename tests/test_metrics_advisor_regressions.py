from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from orchestrator_engine.metrics import contracts
from orchestrator_engine.metrics.advisor import advise
from orchestrator_engine.metrics.cli import run as run_metrics
from orchestrator_engine.metrics.store import MetricsStore


class AdvisorRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = "11111111-1111-4111-8111-111111111111"
        self.worker_source = "22222222-2222-4222-8222-222222222222"
        self.index = 0

    def item(
        self, record_type: str, data: dict[str, object]
    ) -> dict[str, object]:
        self.index += 1
        return contracts.make_observation(
            source_id=self.source,
            record_type=record_type,
            data=data,
            observation_id=f"record-{self.index}",
            observed_at=f"2026-09-08T10:{self.index:02d}:00Z",
            scope={
                "package_id": data.get("package_id"),
                "operation_id": data.get("operation_id"),
            },
        )

    def binding(self) -> dict[str, object]:
        return self.item(
            "classification",
            {
                "classification_id": "binding-new",
                "classification_kind": "package_binding",
                "package_id": "package-1",
                "requirement_set_id": "requirements-new",
                "requirement_ids": ["requirement-1"],
                "obligation_ids": [],
                "obligations_complete": True,
                "candidate_id": "candidate-new",
                "check_plan_revision": "checks-new",
                "policy_revision": "policy-1",
            },
        )

    def final_gate(self) -> dict[str, object]:
        return self.item(
            "execution_attempt",
            {
                "execution_id": "final-new",
                "operation_id": "final-new",
                "package_id": "package-1",
                "requirement_id": "requirement-1",
                "candidate_id": "candidate-new",
                "check_plan_revision": "checks-new",
                "policy_revision": "policy-1",
                "verification_role": "final_gate",
                "status": "completed",
                "accepted": True,
                "authority_status": "owner_authorized",
                "evidence_digest": "a" * 64,
            },
        )

    def failure(
        self, *, candidate_id: str = "candidate-new"
    ) -> dict[str, object]:
        return self.item(
            "execution_attempt",
            {
                "execution_id": f"failed-{candidate_id}",
                "operation_id": f"failed-{candidate_id}",
                "package_id": "package-1",
                "requirement_id": "requirement-1",
                "candidate_id": candidate_id,
                "check_plan_revision": (
                    "checks-new" if candidate_id == "candidate-new" else "checks-old"
                ),
                "policy_revision": "policy-1",
                "status": "failed",
            },
        )

    def test_operation_only_requires_an_observed_operation(self) -> None:
        result = advise(
            [], scope="operation_only", operation_id="missing-operation"
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "operation_context_unavailable")
        self.assertEqual(result["next_action"], "use_ordinary_project_policy")

    def test_completed_focused_operation_does_not_invent_a_final_gate(self) -> None:
        operation = self.item(
            "execution_attempt",
            {
                "execution_id": "focused-1",
                "operation_id": "focused-1",
                "status": "completed",
                "verification_role": "focused",
            },
        )

        result = advise(
            [operation], scope="operation_only", operation_id="focused-1"
        )

        self.assertEqual(result["next_action"], "handoff_operation_result")
        self.assertFalse(result["details"]["check_required"])
        self.assertFalse(result["details"]["publication_eligible"])

    def test_queued_and_unknown_operations_are_not_handed_off_as_complete(self) -> None:
        queued = self.item(
            "execution_attempt",
            {"execution_id": "queued", "operation_id": "queued", "status": "queued"},
        )
        unknown = self.item(
            "execution_attempt",
            {"execution_id": "unknown", "operation_id": "unknown", "status": "mystery"},
        )

        queued_result = advise([queued], scope="operation_only")
        unknown_result = advise([unknown], scope="operation_only")

        self.assertEqual(queued_result["next_action"], "wait_for_running_operation")
        self.assertEqual(
            unknown_result["next_action"], "inspect_incomplete_operation"
        )

    def test_latest_cancelled_retry_supersedes_older_success(self) -> None:
        completed = self.item(
            "execution_attempt",
            {
                "execution_id": "attempt-old",
                "operation_id": "operation-1",
                "status": "completed",
            },
        )
        cancelled = self.item(
            "execution_attempt",
            {
                "execution_id": "attempt-new",
                "operation_id": "operation-1",
                "status": "cancelled",
            },
        )

        result = advise([completed, cancelled], scope="operation_only")

        self.assertEqual(result["next_action"], "inspect_incomplete_operation")
        self.assertFalse(result["details"]["live_readiness"])

    def test_late_old_result_does_not_supersede_newer_cancelled_attempt(self) -> None:
        completed = contracts.make_observation(
            source_id=self.source,
            record_type="execution_attempt",
            data={
                "execution_id": "attempt-old",
                "operation_id": "operation-1",
                "started_at": "2026-09-08T09:00:00Z",
                "finished_at": "2026-09-08T09:01:00Z",
                "status": "completed",
            },
            observation_id="late-old-result",
            observed_at="2026-09-08T09:01:00Z",
            effective_at="2026-09-08T09:01:00Z",
            known_at="2026-09-08T11:00:00Z",
        )
        cancelled = contracts.make_observation(
            source_id=self.source,
            record_type="execution_attempt",
            data={
                "execution_id": "attempt-new",
                "operation_id": "operation-1",
                "started_at": "2026-09-08T10:00:00Z",
                "finished_at": "2026-09-08T10:01:00Z",
                "status": "cancelled",
            },
            observation_id="new-cancelled-result",
            observed_at="2026-09-08T10:01:00Z",
            effective_at="2026-09-08T10:01:00Z",
            known_at="2026-09-08T10:02:00Z",
        )

        result = advise([completed, cancelled], scope="operation_only")

        self.assertEqual(result["next_action"], "inspect_incomplete_operation")
        self.assertFalse(result["details"]["live_readiness"])

    def test_native_operation_ids_are_scoped_by_source(self) -> None:
        running = self.item(
            "execution_attempt",
            {
                "execution_id": "source-a-attempt",
                "operation_id": "shared",
                "status": "running",
            },
        )
        completed = {
            **self.item(
                "execution_attempt",
                {
                    "execution_id": "source-b-attempt",
                    "operation_id": "shared",
                    "status": "completed",
                },
            ),
            "source_id": self.worker_source,
            "observation_id": "source-b-completed",
        }

        result = advise([running, completed], scope="operation_only")

        self.assertEqual(result["next_action"], "wait_for_running_operation")
        self.assertFalse(result["details"]["live_readiness"])

    def test_attempt_without_stable_identity_fails_closed(self) -> None:
        attempt = self.item(
            "execution_attempt",
            {"status": "completed"},
        )

        result = advise([attempt], scope="operation_only")

        self.assertEqual(result["next_action"], "inspect_incomplete_operation")
        self.assertEqual(result["reason"], "operation_attempt_ambiguous")
        self.assertFalse(result["details"]["live_readiness"])

    def test_payload_cannot_self_authorize_package_evidence(self) -> None:
        binding = {
            **self.binding(),
            "source_id": self.worker_source,
            "observation_id": "worker-binding",
        }

        result = advise(
            [binding],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["reason"], "package_context_unavailable")

    def test_non_owner_final_gate_cannot_publish_owner_package(self) -> None:
        worker_gate = {
            **self.final_gate(),
            "source_id": self.worker_source,
            "observation_id": "worker-gate",
        }

        result = advise(
            [self.binding(), worker_gate],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["next_action"], "run_project_required_final_gate")
        self.assertFalse(result["details"]["publication_eligible"])

    def test_old_candidate_obligation_does_not_close_current_manifest(self) -> None:
        binding = self.binding()
        binding["data"] = {
            **binding["data"],
            "obligation_ids": ["review"],
        }
        old_obligation = self.item(
            "obligation",
            {
                "obligation_id": "review",
                "package_id": "package-1",
                "status": "completed",
                "requirement_set_id": "requirements-old",
                "candidate_id": "candidate-old",
                "check_plan_revision": "checks-old",
                "policy_revision": "policy-1",
            },
        )

        result = advise(
            [binding, old_obligation, self.final_gate()],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["pending_obligations"], ["review"])
        self.assertEqual(
            result["next_action"], "complete_next_pending_obligation"
        )
        self.assertFalse(result["details"]["publication_eligible"])

    def test_new_incomplete_binding_does_not_fall_back_to_old_ready_binding(
        self,
    ) -> None:
        ready = self.binding()
        gate = self.final_gate()
        incomplete = self.item(
            "classification",
            {
                "classification_id": "binding-incomplete",
                "classification_kind": "package_binding",
                "package_id": "package-1",
                "requirement_set_id": "requirements-new",
                "requirement_ids": ["requirement-1"],
                "obligation_ids": ["new-review"],
                "obligations_complete": False,
                "candidate_id": "candidate-new",
                "check_plan_revision": "checks-new",
                "policy_revision": "policy-1",
            },
        )

        result = advise(
            [ready, gate, incomplete],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "package_binding_incomplete")

    def test_late_old_binding_does_not_replace_newer_applicable_binding(self) -> None:
        old_ready = self.binding()
        old_ready["effective_at"] = "2026-09-08T09:00:00Z"
        old_ready["known_at"] = "2026-09-08T11:00:00Z"
        new_incomplete = self.item(
            "classification",
            {
                "classification_id": "binding-incomplete",
                "classification_kind": "package_binding",
                "package_id": "package-1",
                "requirement_set_id": "requirements-new",
                "requirement_ids": ["requirement-1"],
                "obligation_ids": ["review"],
                "obligations_complete": False,
                "candidate_id": "candidate-new",
                "check_plan_revision": "checks-new",
                "policy_revision": "policy-1",
            },
        )
        new_incomplete["effective_at"] = "2026-09-08T10:00:00Z"
        new_incomplete["known_at"] = "2026-09-08T10:01:00Z"

        result = advise(
            [old_ready, new_incomplete],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["reason"], "package_binding_incomplete")

    def test_new_unaccepted_gate_attempt_replaces_old_accepted_gate(self) -> None:
        old_gate = self.final_gate()
        old_gate["data"]["started_at"] = "2026-09-08T10:00:00Z"
        new_gate = self.item(
            "execution_attempt",
            {
                **old_gate["data"],
                "execution_id": "final-retry",
                "started_at": "2026-09-08T11:00:00Z",
                "accepted": False,
            },
        )

        result = advise(
            [self.binding(), old_gate, new_gate],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["next_action"], "run_project_required_final_gate")
        self.assertTrue(result["details"]["historical_verification"])
        self.assertFalse(result["details"]["publication_eligible"])

    def test_canonical_complement_cannot_inherit_project_owner_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MetricsStore(root)
            store.initialize()
            for source_id, name, authority in (
                (self.source, "owner", "project_owner"),
                (self.worker_source, "worker", "source_asserted"),
            ):
                store.register_source(
                    name=name,
                    source_type="fixture",
                    source_id=source_id,
                    authority=authority,
                    identity_mapping="canonical_identity_authorized",
                    observation_semantics="mixed",
                    capabilities=["execution"],
                )
            canonical = {"namespace": "project-executions", "id": "gate-1"}
            binding = contracts.make_observation(
                source_id=self.source,
                record_type="classification",
                data={
                    "classification_id": "binding",
                    "classification_kind": "package_binding",
                    "package_id": "package-1",
                    "requirement_set_id": "requirements-1",
                    "requirement_ids": ["requirement-1"],
                    "obligation_ids": [],
                    "obligations_complete": True,
                    "candidate_id": "candidate-1",
                    "check_plan_revision": "checks-1",
                    "policy_revision": "policy-1",
                },
                observation_id="binding",
                observed_at="2026-09-08T09:00:00Z",
                scope={"package_id": "package-1"},
            )
            worker_gate = contracts.make_observation(
                source_id=self.worker_source,
                record_type="execution_attempt",
                data={
                    "execution_id": "worker-gate",
                    "operation_id": "final-gate",
                    "package_id": "package-1",
                    "candidate_id": "candidate-1",
                    "check_plan_revision": "checks-1",
                    "policy_revision": "policy-1",
                    "verification_role": "final_gate",
                    "status": "completed",
                    "accepted": True,
                    "authority_status": "owner_authorized",
                    "evidence_digest": "a" * 64,
                    "started_at": "2026-09-08T10:00:00Z",
                    "canonical_identity": canonical,
                },
                observation_id="worker-gate",
                observed_at="2026-09-08T10:01:00Z",
                scope={"package_id": "package-1", "operation_id": "final-gate"},
            )
            owner_usage = contracts.make_observation(
                source_id=self.source,
                record_type="execution_attempt",
                data={
                    "execution_id": "accounting-mirror",
                    "operation_id": "final-gate",
                    "package_id": "package-1",
                    "observation_mode": "complement",
                    "usage_measurement_status": "complete",
                    "total_tokens": 10,
                    "canonical_identity": canonical,
                },
                observation_id="owner-usage",
                observed_at="2026-09-08T10:02:00Z",
                scope={"package_id": "package-1", "operation_id": "final-gate"},
            )
            store.ingest([binding, worker_gate, owner_usage])
            args = Namespace(
                metrics_command="advise",
                scope="package",
                package_id="package-1",
                operation_id=None,
                format="json",
                evaluation_time="2026-09-08T12:00:00Z",
            )

            untrusted = run_metrics(args, root, state_dir=".orchestrator")

            self.assertEqual(
                untrusted["next_action"], "run_project_required_final_gate"
            )
            self.assertFalse(untrusted["details"]["publication_eligible"])

            owner_gate = contracts.make_observation(
                source_id=self.source,
                record_type="execution_attempt",
                data={
                    **worker_gate["data"],
                    "execution_id": "owner-gate",
                    "started_at": "2026-09-08T11:00:00Z",
                },
                observation_id="owner-gate",
                observed_at="2026-09-08T11:01:00Z",
                scope={"package_id": "package-1", "operation_id": "final-gate"},
            )
            store.ingest([owner_gate])

            trusted = run_metrics(args, root, state_dir=".orchestrator")

            self.assertEqual(trusted["next_action"], "handoff_for_acceptance")
            self.assertTrue(trusted["details"]["publication_eligible"])

    def test_current_candidate_obligation_closes_current_manifest(self) -> None:
        binding = self.binding()
        binding["data"] = {
            **binding["data"],
            "obligation_ids": ["review"],
        }
        obligation = self.item(
            "obligation",
            {
                "obligation_id": "review",
                "package_id": "package-1",
                "status": "completed",
                "requirement_set_id": "requirements-new",
                "candidate_id": "candidate-new",
                "check_plan_revision": "checks-new",
                "policy_revision": "policy-1",
            },
        )

        result = advise(
            [binding, obligation, self.final_gate()],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["pending_obligations"], [])
        self.assertEqual(result["next_action"], "handoff_for_acceptance")
        self.assertTrue(result["details"]["publication_eligible"])

    def test_ambiguous_owner_bindings_fail_closed(self) -> None:
        other = {
            **self.binding(),
            "source_id": self.worker_source,
            "observation_id": "other-binding",
            "data": {
                **self.binding()["data"],
                "candidate_id": "candidate-other",
            },
        }

        result = advise(
            [self.binding(), other],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source, self.worker_source},
        )

        self.assertEqual(result["reason"], "ambiguous_package_binding")
        self.assertEqual(result["status"], "unavailable")

    def test_running_operation_blocks_publication_eligibility(self) -> None:
        running = self.item(
            "execution_attempt",
            {
                "execution_id": "still-running",
                "operation_id": "still-running",
                "package_id": "package-1",
                "status": "running",
            },
        )

        result = advise(
            [self.binding(), self.final_gate(), running],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["next_action"], "wait_for_running_operation")
        self.assertFalse(result["details"]["live_readiness"])
        self.assertFalse(result["details"]["publication_eligible"])

    def test_future_owner_gate_is_not_applied_before_effective_time(self) -> None:
        gate = self.final_gate()
        gate["effective_at"] = "2099-01-01T00:00:00Z"

        result = advise(
            [self.binding(), gate],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
            evaluation_time="2026-09-08T12:00:00Z",
        )

        self.assertEqual(result["next_action"], "run_project_required_final_gate")
        self.assertFalse(result["details"]["publication_eligible"])
        self.assertEqual(result["as_of"], "2026-09-08T12:00:00Z")

    def test_non_owner_failure_disposition_is_ignored(self) -> None:
        resolution = {
            **self.item(
                "classification",
                {
                    "classification_id": "worker-resolution",
                    "classification_kind": "failure_disposition",
                    "package_id": "package-1",
                    "source_execution_id": "failed-candidate-new",
                    "candidate_id": "candidate-new",
                    "status": "resolved",
                },
            ),
            "source_id": self.worker_source,
            "observation_id": "worker-resolution",
        }

        result = advise(
            [self.binding(), self.failure(), self.final_gate(), resolution],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["next_action"], "repair_failed_operation")

    def test_old_failure_does_not_block_a_new_accepted_candidate(self) -> None:
        result = advise(
            [
                self.binding(),
                self.failure(candidate_id="candidate-old"),
                self.final_gate(),
            ],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(result["next_action"], "handoff_for_acceptance")
        self.assertEqual(result["details"]["unresolved_failure_count"], 0)
        self.assertTrue(result["details"]["publication_eligible"])

    def test_current_failure_remains_blocking_until_explicitly_resolved(self) -> None:
        binding = self.binding()
        failure = self.failure()
        gate = self.final_gate()

        unresolved = advise(
            [binding, failure, gate],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )
        resolution = self.item(
            "classification",
            {
                "classification_id": "resolve-failure",
                "classification_kind": "failure_disposition",
                "package_id": "package-1",
                "source_execution_id": "failed-candidate-new",
                "candidate_id": "candidate-new",
                "status": "resolved",
            },
        )
        resolved = advise(
            [binding, failure, gate, resolution],
            scope="package",
            package_id="package-1",
            project_owner_source_ids={self.source},
        )

        self.assertEqual(unresolved["next_action"], "repair_failed_operation")
        self.assertFalse(unresolved["details"]["publication_eligible"])
        self.assertEqual(resolved["next_action"], "handoff_for_acceptance")
        self.assertTrue(resolved["details"]["publication_eligible"])


if __name__ == "__main__":
    unittest.main()
