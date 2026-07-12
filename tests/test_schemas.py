from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from orchestrator_engine import schemas


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

    def test_cli_lists_and_prints_schema(self) -> None:
        command = [sys.executable, "-m", "orchestrator_engine.cli", "schemas"]
        listed = json.loads(subprocess.check_output(command, text=True))
        self.assertEqual(listed["kind"], schemas.KIND)
        printed = json.loads(
            subprocess.check_output([*command, "wake-target"], text=True)
        )
        self.assertEqual(printed["kind"], "ORCHESTRATOR_WAKE_TARGET")
