from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker

from orchestrator_engine import core, local_checks, schemas
from orchestrator_engine import operation_evidence as evidence


class OperationEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = Draft202012Validator(
            schemas.load("operation-evidence"), format_checker=FormatChecker()
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        state = self.root / ".orchestrator"
        state.mkdir()
        (state / "checks.toml").write_text(
            '[suites.gate]\nverification="focused"\n'
            '[[suites.gate.commands]]\nlabel="unit"\nargv='
            + json.dumps([sys.executable, "-c", "print('PRIVATE_BODY')"])
            + "\n",
            encoding="utf-8",
        )
        self.descriptor = local_checks.start_check(
            self.root,
            check_id="check-1",
            suite="gate",
            execution="foreground",
            wake_policy="never",
        )
        self.directory = state / "checks/check-1"
        self.descriptor_path = self.directory / "check.json"
        self.event_path = Path(self.descriptor["event_path"])

    def query(self):
        report = evidence.operation_evidence(self.root, target="check:check-1")
        self.validator.validate(report)
        return report

    def rewrite(self, path, change):
        value = json.loads(path.read_bytes())
        change(value)
        path.write_text(json.dumps(value), encoding="utf-8")

    def refresh_bindings(self):
        self.rewrite(
            self.directory / "evidence.json",
            lambda d: d.update(
                result_sha256=core.sha256_file(
                    self.directory / "verification-result.json"
                )
            ),
        )
        self.rewrite(
            self.event_path,
            lambda d: d.update(
                result_sha256=core.sha256_file(
                    self.directory / "verification-result.json"
                ),
                evidence_sha256=core.sha256_file(self.directory / "evidence.json"),
            ),
        )

    def test_required_producer_fields_are_checked_before_eligibility(self):
        for path, schema_name, role in (
            (self.descriptor_path, "local-check", None),
            (
                self.directory / "verification-result.json",
                "verification-result",
                "result",
            ),
            (self.directory / "evidence.json", "local-check-evidence", "evidence"),
            (self.event_path, "followup-terminal-event", "event"),
            (self.directory / "operation-owner.json", "check-operation-owner", None),
        ):
            original = path.read_bytes()
            for key in schemas.load(schema_name)["required"]:
                if key in {"schema_version", "kind"}:
                    continue
                with self.subTest(schema=schema_name, field=key):
                    path.write_bytes(original)
                    self.rewrite(path, lambda d: d.pop(key))
                    report = self.query()
                    self.assertNotEqual(report["completeness"], "complete")
                    diagnostic = {"code": "unsupported_schema"}
                    if role:
                        diagnostic["artifact_role"] = role
                    self.assertIn(diagnostic, report["omissions"])
            path.write_bytes(original)

    def test_invalid_scalar_shape_blocks_coherently_bound_result(self):
        path = self.directory / "verification-result.json"
        original = path.read_bytes()
        for field, value in (
            ("duration_seconds", -1),
            ("duration_seconds", True),
            ("exit_code", {}),
            ("exit_code", False),
            ("commands", {}),
            ("started_at", "not-a-timestamp"),
        ):
            with self.subTest(field=field, value=value):
                path.write_bytes(original)
                self.rewrite(path, lambda d: d.update({field: value}))
                self.refresh_bindings()
                report = self.query()
                self.assertNotEqual(report["completeness"], "complete")
                self.assertIn(
                    {"code": "unsupported_schema", "artifact_role": "result"},
                    report["omissions"],
                )
                self.assertNotIn(
                    {"code": "digest_mismatch", "artifact_role": "result"},
                    report["errors"],
                )

    def test_invalid_descriptor_scalar_shape_blocks_eligibility(self):
        original = self.descriptor_path.read_bytes()
        for field, value in (
            ("plan", []),
            ("verification", "none"),
            ("created_at", "invalid"),
            ("long_threshold_seconds", 0),
            ("long_threshold_seconds", True),
            ("check_dir", "relative"),
            ("requested_execution", "invalid"),
        ):
            with self.subTest(field=field):
                self.descriptor_path.write_bytes(original)
                self.rewrite(self.descriptor_path, lambda d: d.update({field: value}))
                report = self.query()
                self.assertNotEqual(report["completeness"], "complete")
                self.assertIn({"code": "unsupported_schema"}, report["omissions"])

    def test_existing_conflicting_owner_blocks_native_graph(self):
        path = self.directory / "operation-owner.json"
        original = path.read_bytes()
        for changes in (
            {"operation_type": "github_actions"},
            {"operation_id": "other"},
        ):
            with self.subTest(changes=changes):
                path.write_bytes(original)
                self.rewrite(path, lambda d: d.update(changes))
                report = self.query()
                self.assertEqual(report["completeness"], "unsupported")
                self.assertEqual(report["artifacts"], [])
                self.assertIn({"code": "identity_mismatch"}, report["errors"])

    def test_absent_legacy_owner_is_not_invented(self):
        path = self.directory / "operation-owner.json"
        path.unlink()
        report = self.query()
        self.assertEqual(report["completeness"], "complete")
        self.assertEqual(report["errors"], [])
        self.assertFalse(path.exists())

    def test_invalid_or_unreadable_owner_blocks_eligibility(self):
        path = self.directory / "operation-owner.json"
        self.rewrite(path, lambda d: d.update(schema_version=2))
        self.assertNotEqual(self.query()["completeness"], "complete")
        path.write_text("not json", encoding="utf-8")
        self.assertNotEqual(self.query()["completeness"], "complete")
        original = evidence._Reader.read

        def read(reader, source, role, *, optional=False):
            if source == path:
                reader.error("artifact_unreadable", omission=True)
                return None, "unreadable"
            return original(reader, source, role, optional=optional)

        with mock.patch.object(evidence._Reader, "read", read):
            self.assertNotEqual(self.query()["completeness"], "complete")

    def test_owner_changes_are_included_in_snapshot_fence(self):
        path = self.directory / "operation-owner.json"
        owner = path.read_bytes()
        for first, second in ((owner, None), (None, owner), (owner, owner + b" ")):
            with self.subTest(first=first is not None, second=second is not None):
                count = 0
                original = evidence._Reader.read

                def read(reader, source, role, *, optional=False):
                    nonlocal count
                    if source == path:
                        count += 1
                        raw = first if count == 1 else second
                        return raw, "present" if raw is not None else "missing"
                    return original(reader, source, role, optional=optional)

                with mock.patch.object(evidence._Reader, "read", read):
                    report = self.query()
                self.assertEqual(count, 2)
                self.assertEqual(report["snapshot"]["consistency"], "changed")
                self.assertNotEqual(report["completeness"], "complete")

    def test_large_integer_is_not_misclassified_as_nonfinite(self):
        self.rewrite(
            self.directory / "verification-result.json",
            lambda d: d.update(duration_seconds=10**400),
        )
        self.refresh_bindings()
        self.assertEqual(self.query()["completeness"], "complete")

    def test_native_lineage_and_privacy_without_mutation(self):
        before = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        r = self.query()
        after = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(r["completeness"], "complete")
        self.assertEqual(r["snapshot"]["consistency"], "stable")
        self.assertEqual(r["errors"], [])
        self.assertEqual(
            [a["integrity"] for a in r["artifacts"]],
            ["matched", "matched", "observed_only"],
        )
        self.assertEqual(len(r["artifacts"][0]["retained"]), 2)
        self.assertEqual(r["candidate"]["state"], "unknown")
        encoded = json.dumps(r)
        self.assertNotIn("PRIVATE_BODY", encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("argv", encoded)

    def test_unsupported_preserves_kind_and_reads_nothing(self):
        with mock.patch.object(
            evidence._Reader, "read", side_effect=AssertionError("unexpected read")
        ):
            for kind in ("worker", "ci", "pr"):
                r = evidence.operation_evidence(self.root, target=kind + ":check-1")
                self.assertEqual(r["target"]["kind"], kind)
                self.assertEqual(r["completeness"], "unsupported")
                self.assertEqual(r["artifacts"], [])

    def test_missing_target_does_not_create_source(self):
        root = self.root / "absent"
        r = evidence.operation_evidence(root, target="check:missing")
        self.assertFalse(root.exists())
        self.assertEqual(r["completeness"], "unavailable")
        self.assertEqual([a["role"] for a in r["artifacts"]], list(evidence.ROLES))
        self.assertTrue(all(a["observed"] is None for a in r["artifacts"]))
        self.assertTrue(all(a["presence"] == "missing" for a in r["artifacts"]))

    def test_missing_bytes_keep_both_expected_bindings(self):
        (self.directory / "verification-result.json").unlink()
        r = self.query()
        a = r["artifacts"][0]
        self.assertEqual(a["presence"], "missing")
        self.assertEqual(a["integrity"], "unavailable")
        self.assertIsNone(a["observed"])
        self.assertEqual(len(a["retained"]), 2)
        self.assertEqual(r["completeness"], "partial")

    def test_unreadable_bytes_keep_expected_bindings(self):
        original = evidence._Reader.read

        def read(reader, path, role, *, optional=False):
            if role == "result":
                reader.error("artifact_unreadable", role, omission=True)
                return None, "unreadable"
            return original(reader, path, role, optional=optional)

        with mock.patch.object(evidence._Reader, "read", read):
            a = self.query()["artifacts"][0]
        self.assertIsNone(a["observed"])
        self.assertEqual(a["integrity"], "unavailable")
        self.assertEqual(len(a["retained"]), 2)

    def test_conflicting_bindings_never_override(self):
        self.rewrite(
            self.directory / "evidence.json", lambda d: d.update(result_sha256="a" * 64)
        )
        self.rewrite(
            self.event_path,
            lambda d: d.update(
                evidence_sha256=core.sha256_file(self.directory / "evidence.json")
            ),
        )
        r = self.query()
        self.assertEqual(r["completeness"], "complete")
        self.assertEqual(r["artifacts"][0]["integrity"], "mismatch")
        self.assertIn(
            {"code": "conflicting_retained_bindings", "artifact_role": "result"},
            r["errors"],
        )

    def test_tampered_bytes_are_not_matched(self):
        self.rewrite(
            self.directory / "verification-result.json",
            lambda d: d.update(private_extra="changed"),
        )
        r = self.query()
        self.assertEqual(r["artifacts"][0]["integrity"], "mismatch")

    def test_active_descriptor_with_event_is_unsealed(self):
        self.rewrite(self.descriptor_path, lambda d: d.update(status="running"))
        r = self.query()
        self.assertEqual(r["snapshot"]["consistency"], "unsealed")
        self.assertFalse(r["operation"]["terminal"])
        self.assertNotEqual(r["completeness"], "complete")

    def test_descriptor_or_event_change_detected_without_retry(self):
        for changing_path in (self.descriptor_path, self.event_path):
            with self.subTest(path=changing_path.name):
                counts = {}
                original = evidence._Reader.read

                def read(reader, path, role, *, optional=False):
                    counts[path] = counts.get(path, 0) + 1
                    raw, presence = original(reader, path, role, optional=optional)
                    if path == changing_path and counts[path] == 2 and raw:
                        return raw + b" ", presence
                    return raw, presence

                with mock.patch.object(evidence._Reader, "read", read):
                    r = self.query()
                self.assertEqual(r["snapshot"]["consistency"], "changed")
                self.assertEqual(counts[self.descriptor_path], 2)
                self.assertEqual(counts[self.event_path], 2)
                self.assertEqual(counts[self.directory / "operation-owner.json"], 2)

    def test_future_schema_cannot_donate_bindings(self):
        self.rewrite(self.event_path, lambda d: d.update(schema_version=2))
        r = self.query()
        self.assertEqual(len(r["artifacts"][0]["retained"]), 1)
        self.assertEqual(r["artifacts"][1]["retained"], [])
        self.assertNotEqual(r["completeness"], "complete")

    def test_wrong_identity_cannot_donate_bindings(self):
        self.rewrite(self.event_path, lambda d: d.update(operation_id="other"))
        r = self.query()
        self.assertEqual(r["artifacts"][1]["retained"], [])
        self.assertIn(
            {"code": "identity_mismatch", "artifact_role": "event"}, r["errors"]
        )

    def test_project_id_is_never_inferred(self):
        self.rewrite(self.event_path, lambda d: d.pop("project_id"))
        r = self.query()
        self.assertEqual(r["source"], {"project_id": None, "identity_state": "unknown"})

    def test_invalid_private_project_id_is_not_exported(self):
        for value in ("\nSECRET", "a" * 257, "\ud800", 99):
            self.rewrite(self.event_path, lambda d: d.update(project_id=value))
            r = self.query()
            self.assertIsNone(r["source"]["project_id"])
            self.assertNotIn("SECRET", json.dumps(r))

    def test_escape_path_and_symlink_are_not_read(self):
        outside = self.root / "private.json"
        outside.write_text('{"secret":"PRIVATE_BODY"}', encoding="utf-8")
        self.rewrite(self.descriptor_path, lambda d: d.update(event_path=str(outside)))
        r = self.query()
        self.assertIsNone(r["artifacts"][2]["observed"])
        self.assertIn(
            {"code": "path_outside_state", "artifact_role": "event"}, r["errors"]
        )
        if os.name != "nt":
            (self.directory / "verification-result.json").unlink()
            (self.directory / "verification-result.json").symlink_to(outside)
            self.assertIsNone(self.query()["artifacts"][0]["observed"])

    def test_budget_includes_repeat_reads_and_sentinel(self):
        with mock.patch.object(evidence, "MAX_READ_BYTES", 20):
            r = self.query()
        self.assertNotEqual(r["completeness"], "complete")
        self.assertTrue(
            any(d["code"] == "read_budget_exceeded" for d in r["omissions"])
        )
        size = sum(
            p.stat().st_size
            for p in (
                self.descriptor_path,
                self.directory / "verification-result.json",
                self.directory / "evidence.json",
                self.event_path,
                self.directory / "operation-owner.json",
            )
        )
        with mock.patch.object(evidence, "MAX_READ_BYTES", size):
            r = self.query()
        self.assertEqual(r["snapshot"]["consistency"], "changed")

    def test_diagnostics_truncation_marker_is_inside_limit(self):
        diagnostics, truncated = evidence._diagnostics(
            {("code" + str(i), None) for i in range(30)}
        )
        self.assertTrue(truncated)
        self.assertEqual(len(diagnostics), 16)
        self.assertIn({"code": "diagnostics_truncated"}, diagnostics)

    def test_target_syntax_and_cli_exit_contract(self):
        for target in (
            "check:../x",
            "check:a/b",
            "check:",
            "unknown:x",
            "check:" + "a" * 129,
        ):
            with self.assertRaises(argparse.ArgumentTypeError):
                evidence.target_argument(target)
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        base = [
            sys.executable,
            "-m",
            "orchestrator_engine.cli",
            "--project-root",
            str(self.root),
            "operation",
            "evidence",
            "--target",
        ]
        for target, code in (
            ("check:check-1", 0),
            ("worker:check-1", 0),
            ("check:../x", 2),
        ):
            r = subprocess.run([*base, target], capture_output=True, env=env)
            self.assertEqual(r.returncode, code, r.stderr)
            if code == 0:
                self.assertEqual(
                    json.loads(r.stdout)["target"]["kind"], target.split(":")[0]
                )

    def test_duplicate_json_keys_fail_closed(self):
        self.event_path.write_text(
            '{"schema_version":1,"schema_version":1,"kind":"ORCHESTRATOR_TERMINAL"}'
        )
        r = self.query()
        self.assertNotEqual(r["completeness"], "complete")
        self.assertIn({"code": "invalid_json", "artifact_role": "event"}, r["errors"])

    def test_invalid_required_shape_cannot_claim_complete_lineage(self):
        self.rewrite(
            self.directory / "evidence.json", lambda d: d.update(plan="invalid")
        )
        self.rewrite(
            self.event_path,
            lambda d: d.update(
                evidence_sha256=core.sha256_file(self.directory / "evidence.json")
            ),
        )
        r = self.query()
        self.assertNotEqual(r["completeness"], "complete")
        self.assertIn(
            {"code": "unsupported_schema", "artifact_role": "evidence"}, r["errors"]
        )
        self.assertEqual(len(r["artifacts"][0]["retained"]), 1)

    def test_closed_schema_rejects_inconsistent_null_states_and_unknown_fields(self):
        r = self.query()
        for mutate in (
            lambda d: d.update(private_body="forbidden"),
            lambda d: d["artifacts"][0].update(observed=None),
            lambda d: d["artifacts"][0].update(integrity="unavailable"),
            lambda d: d["source"].update(identity_state="unknown"),
            lambda d: d["candidate"].update(identity={"value": "a" * 40}),
            lambda d: d["artifacts"][0]["retained"][1].update(basis="check_evidence"),
            lambda d: d["errors"].append({"code": "unknown"}),
            lambda d: d["operation"].update(terminal=None),
            lambda d: d["target"].update(operation_id="check-1\n"),
            lambda d: d["artifacts"][0]["retained"][0].update(sha256="a" * 64 + "\n"),
        ):
            value = copy.deepcopy(r)
            mutate(value)
            self.assertTrue(list(self.validator.iter_errors(value)))

    def test_other_shared_check_producer_is_unsupported(self):
        self.descriptor_path.unlink()
        self.rewrite(
            self.directory / "operation-owner.json",
            lambda d: d.update(operation_type="github_actions"),
        )
        r = self.query()
        self.assertEqual(r["target"]["kind"], "check")
        self.assertEqual(r["completeness"], "unsupported")
        self.assertEqual(r["artifacts"], [])

    @unittest.skipIf(os.name == "nt", "symlink privilege is platform dependent")
    def test_configured_symlink_state_directory_keeps_physical_scope(self):
        (self.root / "alias-state").symlink_to(
            self.root / ".orchestrator", target_is_directory=True
        )
        r = evidence.operation_evidence(
            self.root, target="check:check-1", state_dir="alias-state/../alias-state"
        )
        self.validator.validate(r)
        self.assertEqual(r["completeness"], "complete")
        self.assertEqual(r["errors"], [])

    @unittest.skipIf(os.name == "nt", "POSIX FIFO")
    def test_nonregular_artifact_is_not_opened_as_stream(self):
        path = self.directory / "verification-result.json"
        path.unlink()
        os.mkfifo(path)
        r = self.query()
        self.assertEqual(r["artifacts"][0]["presence"], "unreadable")
        self.assertEqual(len(r["artifacts"][0]["retained"]), 2)

    def test_unknown_operation_status_and_schema_are_not_success(self):
        for value in ("future", [], None):
            self.rewrite(self.descriptor_path, lambda d: d.update(status=value))
            r = self.query()
            self.assertEqual(r["operation"]["status"], "unknown")
            self.assertIsNone(r["operation"]["terminal"])
            self.assertNotEqual(r["completeness"], "complete")
