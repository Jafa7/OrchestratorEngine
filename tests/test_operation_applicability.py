from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from orchestrator_engine import (
    core,
    local_checks,
    operation_applicability,
    operation_evidence,
    operation_evidence_v2,
    schemas,
    verification,
)


def write_config(
    root: Path, *, resource: bool = False, script: str = "print('ok')"
) -> None:
    path = root / ".orchestrator" / "checks.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if resource:
        body = '[suites.gate]\nresource_recipe = "verify"\nverification = "focused"\n'
    else:
        body = (
            '[suites.gate]\nverification = "focused"\n'
            '[[suites.gate.commands]]\nlabel = "unit"\nargv = '
            + json.dumps([sys.executable, "-c", script])
            + "\n"
        )
    path.write_text(body, encoding="utf-8")


def declaration(
    path: Path,
    *,
    candidate: str = "candidate-1",
    retry_of: dict | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": operation_applicability.DECLARATION_KIND,
                "project": "sample-project",
                "work": "sample-work",
                "revision": "revision-1",
                "candidate": candidate,
                "criteria": [{"id": "criterion-1", "revision": "revision-1"}],
                "retry_of": retry_of,
            }
        ),
        encoding="utf-8",
    )
    return path


class OperationApplicabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        documents = {name: schemas.load(name) for name in schemas.SCHEMA_NAMES}
        registry = Registry().with_resources(
            (document["$id"], Resource.from_contents(document))
            for document in documents.values()
        )
        cls.validators = {
            name: Draft202012Validator(
                document,
                registry=registry,
                format_checker=FormatChecker(),
            )
            for name, document in documents.items()
        }
        cls.v1_validator = Draft202012Validator(
            documents["operation-evidence"],
            registry=registry,
            format_checker=FormatChecker(),
        )
        cls.v2_validator = Draft202012Validator(
            documents["operation-evidence-v2"],
            registry=registry,
            format_checker=FormatChecker(),
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        write_config(self.root)
        self.input = declaration(self.root / "applicability.json")

    def run_check(self, check_id: str = "CHECK-APP-1", input_path: Path | None = None):
        return local_checks.start_check(
            self.root,
            check_id=check_id,
            suite="gate",
            execution="foreground",
            wake_policy="never",
            applicability_input=input_path or self.input,
        )

    def test_opt_in_preserves_v1_and_returns_closed_v2(self) -> None:
        descriptor = self.run_check()
        directory = self.root / ".orchestrator" / "checks" / "CHECK-APP-1"
        result = core.load_object(directory / "verification-result.json")
        evidence = core.load_object(directory / "evidence.json")
        self.validators["local-check"].validate(
            core.load_object(directory / "check.json")
        )
        self.validators["local-check-evidence"].validate(evidence)
        self.validators["operation-applicability-declaration"].validate(
            core.load_object(directory / "applicability-input.json")
        )
        self.validators["operation-applicability"].validate(
            core.load_object(directory / "applicability.json")
        )
        self.assertEqual(result["applicability"], descriptor["applicability"])
        self.assertEqual(evidence["applicability"], descriptor["applicability"])

        report_v1 = operation_evidence.operation_evidence(
            self.root, target="check:CHECK-APP-1"
        )
        self.v1_validator.validate(report_v1)
        self.assertEqual(report_v1["attempt"]["state"], "unknown")
        self.assertEqual(report_v1["candidate"]["state"], "unknown")

        report_v2 = operation_evidence.operation_evidence(
            self.root, target="check:CHECK-APP-1", contract_version=2
        )
        self.v2_validator.validate(report_v2)
        self.assertEqual(report_v2["completeness"], "complete")
        self.assertEqual(report_v2["applicability"]["state"], "retained")
        self.assertEqual(
            report_v2["applicability"]["assurance"]["executed_candidate"],
            "unknown",
        )
        self.assertEqual(
            report_v2["applicability"]["assurance"]["criteria_fulfillment"],
            "unknown",
        )

    def test_semantic_replay_reuses_attempt_and_change_conflicts(self) -> None:
        first = self.run_check()
        value = json.loads(self.input.read_text())
        self.input.write_text(
            json.dumps(value, indent=2, sort_keys=True), encoding="utf-8"
        )
        replay = self.run_check()
        self.assertTrue(replay["idempotent"])
        self.assertEqual(
            replay["applicability"]["attempt_id"],
            first["applicability"]["attempt_id"],
        )

        declaration(self.input, candidate="candidate-2")
        with self.assertRaisesRegex(local_checks.LocalCheckError, "different options"):
            self.run_check()

    def test_concurrent_same_id_elects_one_attempt(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.run_check(), range(2)))
        attempts = {item["applicability"]["attempt_id"] for item in results}
        self.assertEqual(len(attempts), 1)
        self.assertEqual(sorted(item["idempotent"] for item in results), [False, True])

    def test_invalid_inputs_fail_before_descriptor_publication(self) -> None:
        invalid_values = (
            '{"schema_version":1,"schema_version":1}',
            '{"schema_version":1.0}',
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": operation_applicability.DECLARATION_KIND,
                    "project": "p",
                    "work": "w",
                    "revision": "r",
                    "candidate": "c",
                    "criteria": [{"id": "i", "revision": "r"}],
                    "retry_of": None,
                }
            ),
        )
        for index, body in enumerate(invalid_values):
            with self.subTest(index=index):
                path = self.root / f"invalid-{index}.json"
                path.write_text(body, encoding="utf-8")
                check_id = f"CHECK-INVALID-{index}"
                with self.assertRaises(local_checks.LocalCheckError):
                    self.run_check(check_id, path)
                self.assertFalse(
                    local_checks.descriptor_path(
                        self.root, check_id, state_dir=core.DEFAULT_STATE_DIR
                    ).exists()
                )

        oversized = self.root / "oversized.json"
        oversized.write_bytes(
            b"{" + b" " * operation_applicability.MAX_INPUT_BYTES + b"}"
        )
        with self.assertRaisesRegex(local_checks.LocalCheckError, "size limit"):
            self.run_check("CHECK-OVERSIZED", oversized)

    def test_declaration_rejects_utf16_and_utf32(self) -> None:
        value = json.loads(self.input.read_text(encoding="utf-8"))
        for encoding in ("utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                path = self.root / f"{encoding}.json"
                path.write_bytes(json.dumps(value).encode(encoding))
                with self.assertRaisesRegex(
                    operation_applicability.ApplicabilityError,
                    "strict UTF-8 JSON",
                ):
                    operation_applicability.read_declaration(path)

    @unittest.skipIf(os.name == "nt", "symlink privilege is platform dependent")
    def test_symlink_input_is_rejected(self) -> None:
        link = self.root / "link.json"
        link.symlink_to(self.input)
        with self.assertRaisesRegex(local_checks.LocalCheckError, "regular file"):
            self.run_check("CHECK-SYMLINK", link)

    def test_partial_preparation_requires_explicit_recovery(self) -> None:
        check_id = "CHECK-PARTIAL"
        verification.claim_check_owner(
            self.root, operation_id=check_id, operation_type="local_check"
        )
        directory = self.root / ".orchestrator" / "checks" / check_id
        (directory / "applicability-input.json").write_bytes(self.input.read_bytes())
        with self.assertRaisesRegex(local_checks.LocalCheckError, "explicit recovery"):
            self.run_check(check_id)
        self.assertFalse((directory / "check.json").exists())

    def test_sealed_preparation_is_reused_without_second_attempt(self) -> None:
        check_id = "CHECK-SEALED-PREP"
        verification.claim_check_owner(
            self.root, operation_id=check_id, operation_type="local_check"
        )
        directory = self.root / ".orchestrator" / "checks" / check_id
        spec = local_checks.read_suite(
            self.root, suite="gate", state_dir=core.DEFAULT_STATE_DIR
        )
        prepared = operation_applicability.prepare(
            self.root,
            state_dir=core.DEFAULT_STATE_DIR,
            operation_id=check_id,
            directory=directory,
            suite_fingerprint=spec["fingerprint"],
            declaration_path=self.input,
        )
        self.assertFalse((directory / "check.json").exists())

        completed = self.run_check(check_id)
        self.assertEqual(
            completed["applicability"]["attempt_id"], prepared["attempt_id"]
        )

    def test_legacy_operation_cannot_be_enriched_after_dispatch(self) -> None:
        legacy = local_checks.start_check(
            self.root,
            check_id="CHECK-LEGACY",
            suite="gate",
            execution="foreground",
            wake_policy="never",
        )
        self.assertNotIn("applicability", legacy)
        report = operation_evidence.operation_evidence(
            self.root, target="check:CHECK-LEGACY", contract_version=2
        )
        self.v2_validator.validate(report)
        self.assertEqual(report["completeness"], "unsupported")
        self.assertEqual(report["applicability"]["state"], "unsupported")

        with self.assertRaisesRegex(local_checks.LocalCheckError, "different options"):
            self.run_check("CHECK-LEGACY")

    def test_exact_retry_requires_terminal_pins(self) -> None:
        first = self.run_check("CHECK-FIRST")
        metadata = first["applicability"]
        retry = {
            "operation_kind": "check",
            "operation_id": "CHECK-FIRST",
            "attempt_id": metadata["attempt_id"],
            "applicability_sha256": metadata["artifact_sha256"],
            "source_namespace": metadata["source_namespace"],
            "native_location_binding": metadata["native_location_binding"],
        }
        retry_input = declaration(
            self.root / "retry.json", candidate="candidate-2", retry_of=retry
        )
        second = self.run_check("CHECK-SECOND", retry_input)
        self.assertEqual(
            core.load_object(
                self.root
                / ".orchestrator/checks/CHECK-SECOND"
                / second["applicability"]["artifact_path"]
            )["retry_of"],
            retry,
        )

        predecessor_evidence = (
            self.root / ".orchestrator/checks/CHECK-FIRST/evidence.json"
        )
        retained_evidence = core.load_object(predecessor_evidence)
        retained_evidence["applicability"]["attempt_id"] = (
            "123e4567-e89b-12d3-a456-426614174000"
        )
        core.atomic_json(predecessor_evidence, retained_evidence)
        report = operation_evidence.operation_evidence(
            self.root, target="check:CHECK-SECOND", contract_version=2
        )
        self.assertEqual(report["applicability"]["state"], "conflicted")
        self.assertIn({"code": "applicability_invalid"}, report["errors"])

        core.atomic_json(
            predecessor_evidence,
            {
                **retained_evidence,
                "applicability": first["applicability"],
            },
        )

        wrong = dict(retry)
        wrong["attempt_id"] = "123e4567-e89b-12d3-a456-426614174000"
        wrong_input = declaration(
            self.root / "wrong-retry.json", candidate="candidate-3", retry_of=wrong
        )
        with self.assertRaisesRegex(local_checks.LocalCheckError, "attempt mismatch"):
            self.run_check("CHECK-WRONG-RETRY", wrong_input)

        bad_digest = dict(retry)
        bad_digest["applicability_sha256"] = "0" * 64
        bad_digest_input = declaration(
            self.root / "bad-digest.json",
            candidate="candidate-4",
            retry_of=bad_digest,
        )
        with self.assertRaisesRegex(local_checks.LocalCheckError, "digest mismatch"):
            self.run_check("CHECK-BAD-DIGEST", bad_digest_input)

        first_path = self.root / ".orchestrator" / "checks/CHECK-FIRST/check.json"
        first_descriptor = core.load_object(first_path)
        first_descriptor["status"] = "running"
        core.atomic_json(first_path, first_descriptor)
        nonterminal_input = declaration(
            self.root / "nonterminal.json",
            candidate="candidate-5",
            retry_of=retry,
        )
        with self.assertRaisesRegex(local_checks.LocalCheckError, "not terminal"):
            self.run_check("CHECK-NONTERMINAL", nonterminal_input)

    def test_retry_requires_a_sealed_terminal_graph(self) -> None:
        first = self.run_check("CHECK-SEALED-FIRST")
        metadata = first["applicability"]
        retry = {
            "operation_kind": "check",
            "operation_id": "CHECK-SEALED-FIRST",
            "attempt_id": metadata["attempt_id"],
            "applicability_sha256": metadata["artifact_sha256"],
            "source_namespace": metadata["source_namespace"],
            "native_location_binding": metadata["native_location_binding"],
        }
        result_path = Path(first["result_path"])
        result = core.load_object(result_path)
        result["status"] = "running"
        core.atomic_json(result_path, result)
        retry_input = declaration(
            self.root / "unsealed-retry.json",
            candidate="candidate-unsealed",
            retry_of=retry,
        )
        with self.assertRaisesRegex(local_checks.LocalCheckError, "terminal graph"):
            self.run_check("CHECK-UNSEALED-SECOND", retry_input)

    def test_retry_rejects_broken_terminal_graph_components(self) -> None:
        for index, mode in enumerate(
            ("missing_event", "wrong_event_identity", "wrong_owner", "stale_hash")
        ):
            with self.subTest(mode=mode):
                first_id = f"CHECK-GRAPH-FIRST-{index}"
                second_id = f"CHECK-GRAPH-SECOND-{index}"
                first = self.run_check(first_id)
                metadata = first["applicability"]
                retry = {
                    "operation_kind": "check",
                    "operation_id": first_id,
                    "attempt_id": metadata["attempt_id"],
                    "applicability_sha256": metadata["artifact_sha256"],
                    "source_namespace": metadata["source_namespace"],
                    "native_location_binding": metadata[
                        "native_location_binding"
                    ],
                }
                if mode == "missing_event":
                    Path(first["event_path"]).unlink()
                elif mode == "wrong_event_identity":
                    event_path = Path(first["event_path"])
                    event = core.load_object(event_path)
                    event["operation_id"] = "OTHER-CHECK"
                    core.atomic_json(event_path, event)
                elif mode == "wrong_owner":
                    owner_path = Path(first["check_dir"]) / "operation-owner.json"
                    owner = core.load_object(owner_path)
                    owner["operation_type"] = "resource_check"
                    core.atomic_json(owner_path, owner)
                else:
                    result_path = Path(first["result_path"])
                    result = core.load_object(result_path)
                    result["tampered"] = True
                    core.atomic_json(result_path, result)
                retry_input = declaration(
                    self.root / f"broken-graph-{index}.json",
                    candidate=f"candidate-broken-{index}",
                    retry_of=retry,
                )
                with self.assertRaises(local_checks.LocalCheckError):
                    self.run_check(second_id, retry_input)

    def test_retry_evidence_uses_bounded_contained_predecessor_reads(self) -> None:
        first = self.run_check("CHECK-BOUNDED-FIRST")
        metadata = first["applicability"]
        retry = {
            "operation_kind": "check",
            "operation_id": "CHECK-BOUNDED-FIRST",
            "attempt_id": metadata["attempt_id"],
            "applicability_sha256": metadata["artifact_sha256"],
            "source_namespace": metadata["source_namespace"],
            "native_location_binding": metadata["native_location_binding"],
        }
        retry_input = declaration(
            self.root / "bounded-retry.json",
            candidate="candidate-bounded",
            retry_of=retry,
        )
        self.run_check("CHECK-BOUNDED-SECOND", retry_input)

        result_path = Path(first["result_path"])
        result = core.load_object(result_path)
        result["unused"] = "x" * (operation_evidence_v2.MAX_READ_BYTES + 1)
        core.atomic_json(result_path, result)
        report = operation_evidence.operation_evidence(
            self.root,
            target="check:CHECK-BOUNDED-SECOND",
            contract_version=2,
        )
        self.assertNotEqual(report["completeness"], "complete")
        diagnostics = {
            item["code"] for item in [*report["errors"], *report["omissions"]]
        }
        self.assertIn("read_budget_exceeded", diagnostics)

    @unittest.skipIf(os.name == "nt", "symlink privilege is platform dependent")
    def test_retry_evidence_rejects_predecessor_directory_escape(self) -> None:
        first = self.run_check("CHECK-ESCAPE-FIRST")
        metadata = first["applicability"]
        retry = {
            "operation_kind": "check",
            "operation_id": "CHECK-ESCAPE-FIRST",
            "attempt_id": metadata["attempt_id"],
            "applicability_sha256": metadata["artifact_sha256"],
            "source_namespace": metadata["source_namespace"],
            "native_location_binding": metadata["native_location_binding"],
        }
        retry_input = declaration(
            self.root / "escape-retry.json",
            candidate="candidate-escape",
            retry_of=retry,
        )
        self.run_check("CHECK-ESCAPE-SECOND", retry_input)
        predecessor = self.root / ".orchestrator/checks/CHECK-ESCAPE-FIRST"
        outside = self.root / "outside-predecessor"
        predecessor.rename(outside)
        predecessor.symlink_to(outside, target_is_directory=True)

        report = operation_evidence.operation_evidence(
            self.root,
            target="check:CHECK-ESCAPE-SECOND",
            contract_version=2,
        )
        self.assertEqual(report["applicability"]["state"], "conflicted")
        self.assertIn(
            "path_outside_state", {item["code"] for item in report["errors"]}
        )

    def test_v2_fences_v1_and_applicability_as_one_observation(self) -> None:
        for index, role in enumerate(
            ("result", "evidence", "descriptor", "event", "owner")
        ):
            with self.subTest(role=role):
                check_id = f"CHECK-FENCE-{index}"
                descriptor = self.run_check(check_id)
                paths = {
                    "result": Path(descriptor["result_path"]),
                    "evidence": Path(descriptor["evidence_path"]),
                    "descriptor": Path(descriptor["check_dir"]) / "check.json",
                    "event": Path(descriptor["event_path"]),
                    "owner": Path(descriptor["check_dir"])
                    / "operation-owner.json",
                }
                actual = operation_evidence.operation_evidence
                mutated = False

                def mutate_after_v1(*args, **kwargs):
                    nonlocal mutated
                    response = actual(*args, **kwargs)
                    if kwargs.get("_capture") is not None and not mutated:
                        mutated = True
                        value = core.load_object(paths[role])
                        value["review_mutation"] = role
                        core.atomic_json(paths[role], value)
                    return response

                with mock.patch.object(
                    operation_evidence_v2.v1,
                    "operation_evidence",
                    side_effect=mutate_after_v1,
                ):
                    report = operation_evidence_v2.operation_evidence_v2(
                        self.root, target=f"check:{check_id}"
                    )
                self.assertEqual(report["snapshot"]["consistency"], "changed")
                self.assertEqual(report["completeness"], "conflicted")
                self.assertIn(
                    "snapshot_changed", {item["code"] for item in report["errors"]}
                )

    def test_admission_lock_does_not_cover_foreground_execution(self) -> None:
        started = threading.Event()
        release = threading.Event()
        actual = local_checks.supervise_check

        def blocked(project, *, check_id, state_dir=core.DEFAULT_STATE_DIR):
            started.set()
            self.assertTrue(release.wait(5))
            return actual(project, check_id=check_id, state_dir=state_dir)

        with (
            mock.patch.object(
                local_checks, "supervise_check", side_effect=blocked
            ),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            first = executor.submit(self.run_check, "CHECK-LOCK-SCOPE")
            self.assertTrue(started.wait(2))
            before = time.monotonic()
            replay = self.run_check("CHECK-LOCK-SCOPE")
            elapsed = time.monotonic() - before
            self.assertLess(elapsed, 1.0)
            self.assertTrue(replay["idempotent"])

            declaration(self.input, candidate="candidate-conflict")
            before = time.monotonic()
            with self.assertRaisesRegex(
                local_checks.LocalCheckError, "different options"
            ):
                self.run_check("CHECK-LOCK-SCOPE")
            self.assertLess(time.monotonic() - before, 1.0)
            release.set()
            first.result(timeout=5)

    def test_boolean_producer_versions_are_rejected(self) -> None:
        for field in ("artifact", "descriptor"):
            with self.subTest(field=field):
                check_id = f"CHECK-BOOL-{field.upper()}"
                descriptor = self.run_check(check_id)
                directory = Path(descriptor["check_dir"])
                if field == "artifact":
                    path = directory / "applicability.json"
                    value = core.load_object(path)
                    value["applicability_version"] = True
                else:
                    path = directory / "check.json"
                    value = core.load_object(path)
                    value["applicability"]["producer_version"] = True
                core.atomic_json(path, value)
                with self.assertRaises(operation_applicability.ApplicabilityError):
                    operation_applicability.validate_descriptor_binding(
                        self.root,
                        state_dir=core.DEFAULT_STATE_DIR,
                        descriptor=core.load_object(directory / "check.json"),
                    )

    def test_terminal_tamper_is_conflicted(self) -> None:
        descriptor = self.run_check()
        result_path = Path(descriptor["result_path"])
        result = core.load_object(result_path)
        result["applicability"]["attempt_id"] = (
            "123e4567-e89b-12d3-a456-426614174000"
        )
        core.atomic_json(result_path, result)
        report = operation_evidence.operation_evidence(
            self.root, target="check:CHECK-APP-1", contract_version=2
        )
        self.v2_validator.validate(report)
        self.assertEqual(report["applicability"]["state"], "conflicted")
        self.assertIn({"code": "terminal_binding_mismatch"}, report["errors"])

    def test_v2_reconciles_static_integrity_errors_with_schema(self) -> None:
        descriptor = self.run_check("CHECK-STATIC-INTEGRITY")
        result_path = Path(descriptor["result_path"])
        result = core.load_object(result_path)
        result["tampered"] = True
        core.atomic_json(result_path, result)

        report = operation_evidence.operation_evidence(
            self.root,
            target="check:CHECK-STATIC-INTEGRITY",
            contract_version=2,
        )
        self.v2_validator.validate(report)
        self.assertEqual(report["completeness"], "conflicted")
        self.assertIn("digest_mismatch", {item["code"] for item in report["errors"]})

    def test_terminal_metadata_types_are_validated_with_coherent_hashes(self) -> None:
        first = self.run_check("CHECK-TYPED-TERMINAL")
        result_path = Path(first["result_path"])
        evidence_path = Path(first["evidence_path"])
        event_path = Path(first["event_path"])
        result = core.load_object(result_path)
        evidence = core.load_object(evidence_path)
        result["applicability"]["producer_version"] = True
        evidence["applicability"]["producer_version"] = True
        core.atomic_json(result_path, result)
        evidence["result_sha256"] = core.sha256_file(result_path)
        core.atomic_json(evidence_path, evidence)
        event = core.load_object(event_path)
        event["result_sha256"] = core.sha256_file(result_path)
        event["evidence_sha256"] = core.sha256_file(evidence_path)
        core.atomic_json(event_path, event)

        report = operation_evidence.operation_evidence(
            self.root,
            target="check:CHECK-TYPED-TERMINAL",
            contract_version=2,
        )
        self.v2_validator.validate(report)
        self.assertEqual(report["completeness"], "conflicted")
        self.assertIn(
            "terminal_binding_mismatch",
            {item["code"] for item in report["errors"]},
        )
        self.assertIsNone(
            local_checks.recover_completed_check(
                self.root,
                core.load_object(Path(first["check_dir"]) / "check.json"),
                state_dir=core.DEFAULT_STATE_DIR,
            )
        )

        metadata = first["applicability"]
        retry = {
            "operation_kind": "check",
            "operation_id": "CHECK-TYPED-TERMINAL",
            "attempt_id": metadata["attempt_id"],
            "applicability_sha256": metadata["artifact_sha256"],
            "source_namespace": metadata["source_namespace"],
            "native_location_binding": metadata["native_location_binding"],
        }
        retry_input = declaration(
            self.root / "typed-terminal-retry.json",
            candidate="candidate-typed-terminal",
            retry_of=retry,
        )
        with self.assertRaisesRegex(local_checks.LocalCheckError, "terminal binding"):
            self.run_check("CHECK-TYPED-RETRY", retry_input)

    def test_missing_or_future_artifact_is_conflicted(self) -> None:
        for mode in ("missing", "future"):
            with self.subTest(mode=mode):
                check_id = f"CHECK-{mode.upper()}"
                descriptor = self.run_check(check_id)
                path = (
                    self.root
                    / ".orchestrator/checks"
                    / check_id
                    / descriptor["applicability"]["artifact_path"]
                )
                if mode == "missing":
                    path.unlink()
                else:
                    artifact = core.load_object(path)
                    artifact["applicability_version"] = 2
                    core.atomic_json(path, artifact)
                report = operation_evidence.operation_evidence(
                    self.root, target=f"check:{check_id}", contract_version=2
                )
                self.v2_validator.validate(report)
                self.assertEqual(report["applicability"]["state"], "conflicted")

    def test_all_terminal_paths_preserve_binding(self) -> None:
        write_config(self.root, script="raise SystemExit(7)")
        failed = self.run_check("CHECK-FAILED")
        failed_dir = self.root / ".orchestrator" / "checks/CHECK-FAILED"
        for name in ("verification-result.json", "evidence.json"):
            self.assertEqual(
                core.load_object(failed_dir / name)["applicability"],
                failed["applicability"],
            )

        with self.assertRaisesRegex(local_checks.LocalCheckError, "could not launch"):
            local_checks.start_check(
                self.root,
                check_id="CHECK-LAUNCH-ERROR",
                suite="gate",
                execution="detached",
                wake_policy="never",
                applicability_input=self.input,
                popen_factory=mock.Mock(side_effect=OSError("synthetic launch error")),
            )
        launch_dir = self.root / ".orchestrator" / "checks/CHECK-LAUNCH-ERROR"
        launch_descriptor = core.load_object(launch_dir / "check.json")
        for name in ("verification-result.json", "evidence.json"):
            self.assertEqual(
                core.load_object(launch_dir / name)["applicability"],
                launch_descriptor["applicability"],
            )

    def test_supervisor_revalidates_before_first_command(self) -> None:
        marker = self.root / "must-not-run"
        write_config(
            self.root,
            script=f"from pathlib import Path; Path({str(marker)!r}).touch()",
        )
        actual_supervise = local_checks.supervise_check

        def retain_starting(project, *, check_id, state_dir):
            return core.load_object(
                local_checks.descriptor_path(project, check_id, state_dir=state_dir)
            )

        with mock.patch.object(local_checks, "supervise_check", retain_starting):
            descriptor = self.run_check("CHECK-REVALIDATE")
        artifact_path = (
            self.root
            / ".orchestrator/checks/CHECK-REVALIDATE"
            / descriptor["applicability"]["artifact_path"]
        )
        artifact = core.load_object(artifact_path)
        artifact["request_fingerprint"] = "0" * 64
        core.atomic_json(artifact_path, artifact)

        final = actual_supervise(self.root, check_id="CHECK-REVALIDATE")
        self.assertEqual(final["status"], "errored")
        self.assertFalse(marker.exists())

    def test_relocation_is_a_conflict_not_history_rewrite(self) -> None:
        self.run_check()
        with tempfile.TemporaryDirectory() as temporary:
            relocated = Path(temporary) / "relocated"
            shutil.copytree(self.root, relocated)
            report = operation_evidence.operation_evidence(
                relocated, target="check:CHECK-APP-1", contract_version=2
            )
        self.v2_validator.validate(report)
        self.assertEqual(report["applicability"]["state"], "conflicted")
        self.assertEqual(
            report["applicability"]["source"]["binding_state"], "mismatch"
        )
        self.assertIn({"code": "source_mismatch"}, report["errors"])

    def test_resource_managed_opt_in_is_rejected_before_submission(self) -> None:
        write_config(self.root, resource=True)
        with (
            mock.patch(
                "orchestrator_engine.resource_checks.start",
                side_effect=AssertionError("resource submission must not run"),
            ) as start,
            self.assertRaisesRegex(local_checks.LocalCheckError, "unsupported"),
        ):
            self.run_check("CHECK-RESOURCE")
        start.assert_not_called()

    def test_cli_contract_version_defaults_to_v1_and_accepts_v2(self) -> None:
        self.run_check()
        from orchestrator_engine import cli

        parser = cli.build_parser()
        default = parser.parse_args(
            ["operation", "evidence", "--target", "check:CHECK-APP-1"]
        )
        explicit = parser.parse_args(
            [
                "operation",
                "evidence",
                "--target",
                "check:CHECK-APP-1",
                "--contract-version",
                "2",
            ]
        )
        self.assertEqual(default.contract_version, 1)
        self.assertEqual(explicit.contract_version, 2)

    def test_cli_forwards_applicability_and_selects_v2(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        base = [
            sys.executable,
            "-m",
            "orchestrator_engine.cli",
            "--project-root",
            str(self.root),
        ]
        dispatched = subprocess.run(
            [
                *base,
                "check",
                "run",
                "--check-id",
                "CHECK-CLI-APP",
                "--suite",
                "gate",
                "--execution",
                "foreground",
                "--wake-policy",
                "never",
                "--applicability-input",
                str(self.input),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        self.assertIn("applicability", json.loads(dispatched.stdout))

        queried = subprocess.run(
            [
                *base,
                "operation",
                "evidence",
                "--target",
                "check:CHECK-CLI-APP",
                "--contract-version",
                "2",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(queried.returncode, 0, queried.stderr)
        report = json.loads(queried.stdout)
        self.v2_validator.validate(report)
        self.assertEqual(report["applicability"]["state"], "retained")


if __name__ == "__main__":
    unittest.main()
