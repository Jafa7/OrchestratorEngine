from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from orchestrator_engine import conformance, schemas


class SchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = {name: schemas.load(name) for name in schemas.SCHEMA_NAMES}
        cls.registry = Registry().with_resources(
            (document["$id"], Resource.from_contents(document))
            for document in cls.documents.values()
        )
        cls.validators = {
            name: Draft202012Validator(
                document,
                registry=cls.registry,
                format_checker=FormatChecker(),
            )
            for name, document in cls.documents.items()
        }

    def test_catalog_and_each_schema_are_packaged_draft_2020_12(self) -> None:
        catalog = schemas.catalog()
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(catalog["kind"], schemas.KIND)
        self.assertEqual(catalog["schema_count"], len(schemas.SCHEMA_NAMES))
        self.assertEqual(catalog["schemas"], list(schemas.SCHEMA_NAMES))
        for name, document in self.documents.items():
            self.assertEqual(
                document["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            self.assertEqual(document["schema_version"], 1)
            self.assertIn("kind", document)
            Draft202012Validator.check_schema(document)
            self.assertIsNotNone(self.validators[name])

    def test_valid_fixtures_conform_with_external_ref_registry(self) -> None:
        root = Path(__file__).parent / "fixtures" / "schemas" / "valid"
        for name in schemas.SCHEMA_NAMES:
            with self.subTest(name=name):
                fixture = json.loads(
                    (root / f"{name}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(list(self.validators[name].iter_errors(fixture)), [])

    def test_invalid_fixtures_and_required_mutations_are_rejected(self) -> None:
        root = Path(__file__).parent / "fixtures" / "schemas"
        invalid_root = root / "invalid"
        for path in sorted(invalid_root.glob("*.json")):
            name = path.stem
            with self.subTest(invalid_fixture=name):
                invalid = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(list(self.validators[name].iter_errors(invalid)))

        valid_root = root / "valid"
        for name, document in self.documents.items():
            valid = json.loads(
                (valid_root / f"{name}.json").read_text(encoding="utf-8")
            )
            mutations = {
                "schema_version": {**copy.deepcopy(valid), "schema_version": 2},
                "kind": {**copy.deepcopy(valid), "kind": "INVALID_KIND"},
            }
            required_field = document["required"][0]
            without_required = copy.deepcopy(valid)
            without_required.pop(required_field)
            mutations["required"] = without_required
            for label, invalid in mutations.items():
                with self.subTest(schema=name, mutation=label):
                    self.assertTrue(list(self.validators[name].iter_errors(invalid)))

    def test_external_wake_target_ref_uses_the_packaged_registry(self) -> None:
        root = Path(__file__).parent / "fixtures" / "schemas" / "valid"
        event = json.loads((root / "terminal-event.json").read_text(encoding="utf-8"))
        event["wake_target"] = json.loads(
            (root / "wake-target.json").read_text(encoding="utf-8")
        )
        self.assertEqual(list(self.validators["terminal-event"].iter_errors(event)), [])

        event["wake_target"].pop("captured_at")
        self.assertTrue(list(self.validators["terminal-event"].iter_errors(event)))

    def test_worker_policy_ref_uses_the_packaged_registry(self) -> None:
        root = Path(__file__).parent / "fixtures" / "schemas" / "valid"
        policy = json.loads(
            (root / "worker-policy-snapshot.json").read_text(encoding="utf-8")
        )
        for name in ("worker-task", "worker-evidence"):
            with self.subTest(schema=name):
                artifact = json.loads(
                    (root / f"{name}.json").read_text(encoding="utf-8")
                )
                artifact["worker_policy"] = copy.deepcopy(policy)
                self.assertEqual(
                    list(self.validators[name].iter_errors(artifact)),
                    [],
                )
                artifact["worker_policy"]["files"][0]["sha256"] = "invalid"
                self.assertTrue(
                    list(self.validators[name].iter_errors(artifact))
                )

    def test_conformance_report_enforces_failure_and_fixture_disposition(self) -> None:
        path = (
            Path(__file__).parent
            / "fixtures"
            / "schemas"
            / "valid"
            / "conformance-report.json"
        )
        report = json.loads(path.read_text(encoding="utf-8"))

        failed_without_reason = {**copy.deepcopy(report), "status": "failed"}
        self.assertTrue(
            list(
                self.validators["conformance-report"].iter_errors(
                    failed_without_reason
                )
            )
        )

        retained_without_path = copy.deepcopy(report)
        retained_without_path["fixture"] = {
            "status": "retained",
            "root": None,
            "reason": "requested",
        }
        self.assertTrue(
            list(
                self.validators["conformance-report"].iter_errors(
                    retained_without_path
                )
            )
        )

        passed_without_adoption = copy.deepcopy(report)
        passed_without_adoption["adoption_summary"] = {
            "status": "not_run",
            "reason": "earlier_step_failed",
        }
        self.assertTrue(
            list(
                self.validators["conformance-report"].iter_errors(
                    passed_without_adoption
                )
            )
        )

        portable_claiming_concurrency = copy.deepcopy(report)
        portable_claiming_concurrency["concurrency_summary"] = {
            "status": "passed",
            "task_count": 6,
            "wait_any_terminal_count": 1,
            "wait_all_terminal_count": 6,
            "expected_host_counts": {"codex": 3, "vscode": 3},
            "delivered_host_counts": {"codex": 3, "vscode": 3},
        }
        self.assertTrue(
            list(
                self.validators["conformance-report"].iter_errors(
                    portable_claiming_concurrency
                )
            )
        )

        portable_claiming_lifecycle_recovery = copy.deepcopy(report)
        portable_claiming_lifecycle_recovery["lifecycle_recovery_summary"] = {
            "status": "passed",
            "reaped_count": 1,
            "second_reaped_count": 0,
            "terminal_status": "failed",
            "failure_class": "supervisor_lost",
        }
        self.assertTrue(
            list(
                self.validators["conformance-report"].iter_errors(
                    portable_claiming_lifecycle_recovery
                )
            )
        )

        full = copy.deepcopy(report)
        full["requested_mode"] = "full"
        full["effective_mode"] = "full"
        full["concurrency_summary"] = {
            "status": "passed",
            "task_count": 6,
            "wait_any_terminal_count": 1,
            "wait_all_terminal_count": 6,
            "expected_host_counts": {"codex": 3, "vscode": 3},
            "delivered_host_counts": {"codex": 3, "vscode": 3},
        }
        full["lifecycle_recovery_summary"] = {
            "status": "passed",
            "reaped_count": 1,
            "second_reaped_count": 0,
            "terminal_status": "failed",
            "failure_class": "supervisor_lost",
        }
        self.assertEqual(
            list(self.validators["conformance-report"].iter_errors(full)),
            [],
        )

        repeated_recovery = copy.deepcopy(full)
        repeated_recovery["recovery_summary"]["scenarios"][3] = {
            "name": "event_without_signal",
            "status": "recovered",
            "event_id": "different-event-id",
        }
        self.assertTrue(
            list(
                self.validators["conformance-report"].iter_errors(
                    repeated_recovery
                )
            )
        )

        wrong_host_partition = copy.deepcopy(full)
        wrong_host_partition["concurrency_summary"]["delivered_host_counts"] = {
            "codex": 4,
            "vscode": 2,
        }
        self.assertTrue(
            list(
                self.validators["conformance-report"].iter_errors(
                    wrong_host_partition
                )
            )
        )

    def test_runtime_conformance_reports_match_packaged_schema(self) -> None:
        passed = conformance.run_conformance(mode="portable")
        with tempfile.TemporaryDirectory() as existing:
            failed = conformance.run_conformance(
                mode="portable",
                fixture_root=Path(existing),
            )

        validator = self.validators["conformance-report"]
        self.assertEqual(list(validator.iter_errors(passed)), [])
        self.assertEqual(list(validator.iter_errors(failed)), [])

    def test_github_actions_schemas_accept_pre_run_discovery(self) -> None:
        valid = Path(__file__).parent / "fixtures" / "schemas" / "valid"
        monitor = json.loads(
            (valid / "github-actions-monitor.json").read_text(encoding="utf-8")
        )
        monitor.update(
            {
                "run_id": None,
                "requested_run_id": None,
                "attempt": None,
                "expected_head_sha": "a" * 40,
                "workflow_name": "CI",
            }
        )
        evidence = json.loads(
            (valid / "github-actions-evidence.json").read_text(encoding="utf-8")
        )
        evidence.update(
            {
                "run_id": None,
                "monitor_status": "timed_out",
                "ci_conclusion": None,
                "failure_kind": "run_discovery_timeout",
                "discovery": {
                    "status": "timed_out",
                    "query_count": 3,
                    "duration_seconds": 15.0,
                },
            }
        )

        self.assertEqual(
            list(self.validators["github-actions-monitor"].iter_errors(monitor)),
            [],
        )
        self.assertEqual(
            list(self.validators["github-actions-evidence"].iter_errors(evidence)),
            [],
        )

        short_sha_monitor = dict(monitor)
        short_sha_monitor["expected_head_sha"] = "a" * 12
        missing_discovery_evidence = dict(evidence)
        missing_discovery_evidence.pop("discovery")
        self.assertTrue(
            list(
                self.validators["github-actions-monitor"].iter_errors(
                    short_sha_monitor
                )
            )
        )
        self.assertTrue(
            list(
                self.validators["github-actions-evidence"].iter_errors(
                    missing_discovery_evidence
                )
            )
        )

    def test_cli_lists_and_prints_schema(self) -> None:
        command = [sys.executable, "-m", "orchestrator_engine.cli", "schemas"]
        listed = json.loads(subprocess.check_output(command, text=True))
        self.assertEqual(listed["kind"], schemas.KIND)
        printed = json.loads(
            subprocess.check_output([*command, "wake-target"], text=True)
        )
        self.assertEqual(printed["kind"], "ORCHESTRATOR_WAKE_TARGET")
