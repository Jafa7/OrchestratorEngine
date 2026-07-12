from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator_engine import core, worker_policy


class WorkerPolicyTests(unittest.TestCase):
    def test_load_policy_resolves_files_from_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / ".orchestrator" / "workers.toml"
            policy_file = config.parent / "policies" / "base.md"
            policy_file.parent.mkdir(parents=True)
            policy_file.write_text("quality first\n", encoding="utf-8")
            policies = worker_policy.load_policies(
                config,
                {
                    "default": {
                        "files": ["policies/base.md"],
                        "context_strategy": "progressive",
                    }
                },
            )

        self.assertEqual(policies["default"]["files"], [policy_file.resolve()])
        self.assertEqual(
            policies["default"]["metadata"]["context_strategy"],
            "progressive",
        )

    def test_policy_paths_cannot_escape_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / ".orchestrator" / "workers.toml"
            for path in ("../private.md", "/tmp/private.md"):
                with (
                    self.subTest(path=path),
                    self.assertRaises(worker_policy.WorkerPolicyError),
                ):
                    worker_policy.load_policies(
                        config,
                        {"default": {"files": [path]}},
                    )

    def test_policy_control_data_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / ".orchestrator" / "workers.toml"
            cases = {
                "too-many-files": {
                    "files": [
                        f"policies/{index}.md"
                        for index in range(worker_policy.MAX_POLICY_FILES + 1)
                    ]
                },
                "large-metadata": {
                    "files": ["policies/base.md"],
                    "description": "x" * (
                        worker_policy.MAX_POLICY_METADATA_BYTES + 1
                    ),
                },
            }
            for name, value in cases.items():
                with (
                    self.subTest(name=name),
                    self.assertRaises(worker_policy.WorkerPolicyError),
                ):
                    worker_policy.load_policies(config, {name: value})

    def test_snapshot_composes_bounded_auditable_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            config_root = project / ".orchestrator"
            policy_file = config_root / "policies" / "base.md"
            policy_file.parent.mkdir(parents=True)
            policy_file.write_text("inspect narrowly\n", encoding="utf-8")
            prompt = project / "task.md"
            prompt.write_text("fix the issue\n", encoding="utf-8")
            policy = worker_policy.load_policies(
                config_root / "workers.toml",
                {"default": {"files": ["policies/base.md"]}},
            )["default"]
            snapshot = worker_policy.snapshot_prompt(
                project,
                prompt_file=prompt,
                task_dir=project / ".orchestrator" / "tasks" / "T-1",
                policy=policy,
            )
            effective = Path(snapshot["effective_prompt_file"])
            content = effective.read_text(encoding="utf-8")
            effective_hash = core.sha256_file(effective)
            policy_hash = core.sha256_file(policy_file)

            self.assertIn("ORCHESTRATOR_WORKER_POLICY v1", content)
            self.assertIn("inspect narrowly", content)
            self.assertIn("BEGIN_TASK_INPUT", content)
            self.assertIn("fix the issue", content)
            self.assertEqual(snapshot["effective_prompt_sha256"], effective_hash)
            self.assertEqual(snapshot["worker_policy"]["name"], "default")
            self.assertEqual(
                snapshot["worker_policy"]["files"][0]["sha256"],
                policy_hash,
            )

    def test_snapshot_rejects_accidentally_large_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            config_root = project / ".orchestrator"
            policy_file = config_root / "policies" / "large.md"
            policy_file.parent.mkdir(parents=True)
            policy_file.write_bytes(b"x" * (worker_policy.MAX_POLICY_FILE_BYTES + 1))
            prompt = project / "task.md"
            prompt.write_text("task\n", encoding="utf-8")
            policy = worker_policy.load_policies(
                config_root / "workers.toml",
                {"default": {"files": ["policies/large.md"]}},
            )["default"]

            with self.assertRaises(worker_policy.WorkerPolicyError):
                worker_policy.snapshot_prompt(
                    project,
                    prompt_file=prompt,
                    task_dir=project / ".orchestrator" / "tasks" / "T-1",
                    policy=policy,
                )

    def test_snapshotted_effective_prompt_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "task.md"
            effective = root / "effective.md"
            original.write_text("task\n", encoding="utf-8")
            effective.write_text("composed\n", encoding="utf-8")
            descriptor = {
                "effective_prompt_file": str(effective),
                "effective_prompt_sha256": "0" * 64,
            }

            with self.assertRaises(worker_policy.WorkerPolicyError):
                worker_policy.load_snapshotted_prompt(
                    descriptor,
                    original_prompt=original,
                )


if __name__ == "__main__":
    unittest.main()
