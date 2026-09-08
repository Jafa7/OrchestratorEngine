from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from orchestrator_engine import binding, cli, core, schemas, watcher, workstreams


class WorkstreamPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        binding.write_binding(self.root, host="codex", target_thread_id="owner")

    def start(self, **limits):
        return workstreams.start_workstream(
            self.root,
            workstream_id="W",
            goal="Accepted plan",
            delay_seconds=0,
            **limits,
        )

    def checkpoint(self, name, decision="continue", **kwargs):
        return workstreams.checkpoint_workstream(
            self.root,
            workstream_id="W",
            checkpoint_id=name,
            decision=decision,
            summary="Durable progress",
            next_action="Finish the accepted plan",
            ready=decision == "continue",
            **kwargs,
        )

    def update(self, **kwargs):
        return workstreams.set_workstream_policy(
            self.root,
            workstream_id="W",
            reason="Owner requested unlimited work",
            **kwargs,
        )

    def test_unlimited_survives_old_count_and_age_thresholds(self):
        descriptor = self.start()
        self.assertIsNone(descriptor["max_continuations"])
        self.assertIsNone(descriptor["max_wall_seconds"])
        for index in range(10):
            self.checkpoint(f"C{index}")
        # Model an imported long-lived descriptor without resetting its history.
        path = workstreams.descriptor_path(self.root, "W")
        descriptor = core.load_object(path)
        descriptor.update(continuation_count=100, created_at="2000-01-01T00:00:00Z")
        core.atomic_json(path, descriptor)
        checkpoint = self.checkpoint("C100")
        scan = watcher.scan_once(
            [self.root], state_path=self.root / "watcher.json", action="record"
        )
        current = workstreams.load_workstream(self.root, "W")
        self.assertEqual(current["continuation_count"], 101)
        self.assertEqual(current["checkpoint_count"], 11)
        self.assertEqual(current["status"], "active")
        self.assertIn(
            checkpoint["event_id"], [s["event_id"] for s in scan["new_signals"]]
        )

    def test_explicit_old_limits_remain_effective(self):
        self.start(max_continuations=1, max_wall_seconds=14400)
        self.checkpoint("first")
        limited = self.checkpoint("second")
        self.assertEqual(limited["decision"], "needs_user")
        self.assertNotIn("followup", limited)
        self.update(limits={"max_continuations": None, "max_wall_seconds": None})
        self.assertEqual(
            workstreams.load_workstream(self.root, "W")["status"], "needs_user"
        )
        workstreams.resume_workstream(self.root, workstream_id="W")
        self.assertEqual(self.checkpoint("third")["decision"], "continue")

    def test_update_preserves_pending_event_and_delivers_once(self):
        self.start(max_continuations=8, max_wall_seconds=14400)
        checkpoint = self.checkpoint("pending")
        before = workstreams.load_workstream(self.root, "W")
        with mock.patch.object(workstreams, "_ensure_checkpoint_event") as emit:
            self.update(limits={"max_continuations": None, "max_wall_seconds": None})
            emit.assert_not_called()
        after = workstreams.load_workstream(self.root, "W")
        for field in (
            "workstream_id",
            "created_at",
            "checkpoint_count",
            "continuation_count",
            "active_continuation",
            "wake_target",
            "latest_checkpoint_path",
        ):
            self.assertEqual(before[field], after[field])
        first = watcher.scan_once(
            [self.root], state_path=self.root / "watcher.json", action="record"
        )
        second = watcher.scan_once(
            [self.root], state_path=self.root / "watcher.json", action="record"
        )
        self.assertEqual(first["new_count"], 1)
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(
            after["active_continuation"]["event_id"], checkpoint["event_id"]
        )

    def test_waiting_external_retains_provider_operation_and_next_action(self):
        self.start(max_continuations=8, max_wall_seconds=14400)
        checkpoint = self.checkpoint(
            "quota", "waiting_external", waiting_on="worker:CAPACITY-1"
        )
        before = workstreams.load_workstream(self.root, "W")
        self.update(limits={"max_continuations": None, "max_wall_seconds": None})
        after = workstreams.load_workstream(self.root, "W")
        self.assertEqual(after["status"], "waiting_external")
        self.assertEqual(after["waiting_on"], "worker:CAPACITY-1")
        self.assertEqual(after["continuation_count"], before["continuation_count"])
        self.assertEqual(
            core.load_object(Path(checkpoint["checkpoint_path"])),
            {
                k: v
                for k, v in checkpoint.items()
                if k not in {"checkpoint_path", "idempotent"}
            },
        )
        # A verified availability terminal wake lets the same owner explicitly resume.
        workstreams.resume_workstream(self.root, workstream_id="W")
        self.assertEqual(self.checkpoint("capacity-restored")["decision"], "continue")
        self.assertEqual(
            workstreams.load_workstream(self.root, "W")["workstream_id"], "W"
        )

    def test_policy_change_does_not_revive_cancelled_or_completed_work(self):
        self.start(max_continuations=8)
        old = self.checkpoint("old")
        self.checkpoint("stop", "paused")
        self.update(limits={"max_continuations": None})
        signal = core.load_object(Path(old["signal_path"]))
        with workstreams.continuation_delivery_guard(self.root, signal) as guard:
            self.assertFalse(guard["deliver"])
        self.assertEqual(
            workstreams.load_workstream(self.root, "W")["status"], "paused"
        )
        self.checkpoint("done", "complete")
        self.update(limits={"max_wall_seconds": 1000000})
        self.assertEqual(
            workstreams.load_workstream(self.root, "W")["status"], "complete"
        )
        with self.assertRaises(workstreams.WorkstreamError):
            workstreams.resume_workstream(self.root, workstream_id="W")

    def test_policy_revision_prevents_lost_update_and_unchanged_is_idempotent(self):
        self.start(max_continuations=8)

        def change(value):
            try:
                return self.update(
                    limits={"max_continuations": value}, expected_revision=0
                )["status"]
            except workstreams.WorkstreamError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(change, [20, 30]))
        self.assertCountEqual(results, ["updated", "conflict"])
        descriptor = workstreams.load_workstream(self.root, "W")
        repeated = self.update(
            limits={"max_continuations": descriptor["max_continuations"]}
        )
        self.assertEqual(repeated["status"], "unchanged")
        self.assertEqual(len(descriptor["policy_history"]), 1)
        self.assertEqual(
            descriptor["policy_history"][0]["before"]["max_continuations"], 8
        )

    def test_policy_write_failure_preserves_all_durable_state(self):
        self.start(max_continuations=8)
        self.checkpoint("pending")
        before = workstreams.load_workstream(self.root, "W")
        with mock.patch.object(
            core, "atomic_json", side_effect=OSError("disk failure")
        ), self.assertRaises(OSError):
            self.update(limits={"max_continuations": None})
        self.assertEqual(workstreams.load_workstream(self.root, "W"), before)

    def test_finite_limits_have_no_arbitrary_upper_bound_and_invalid_values_fail(self):
        self.start(max_continuations=1000, max_wall_seconds=1000000)
        for value in (0, -1, True, 1.5, "unlimited"):
            with (
                self.subTest(value=value),
                self.assertRaises(workstreams.WorkstreamError),
            ):
                self.update(limits={"max_continuations": value})

    def test_schema_accepts_unlimited_and_legacy_descriptors(self):
        descriptor = self.start()
        descriptor.pop("wake_target")
        validator = Draft202012Validator(schemas.load("workstream"))
        self.assertEqual(list(validator.iter_errors(descriptor)), [])
        descriptor.update(max_continuations=8, max_wall_seconds=14400)
        self.assertEqual(list(validator.iter_errors(descriptor)), [])
        descriptor.update(max_continuations=1000, max_wall_seconds=1000000)
        self.assertEqual(list(validator.iter_errors(descriptor)), [])
        descriptor["max_continuations"] = 0
        self.assertTrue(list(validator.iter_errors(descriptor)))

    def test_cli_can_update_both_or_individual_limits(self):
        def run(*args):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--project-root", str(self.root), "workstream", *args])
            self.assertEqual(code, 0)
            return json.loads(output.getvalue())

        run("start", "--workstream-id", "W", "--goal", "Accepted plan", "--unlimited")
        run(
            "set-policy",
            "--workstream-id",
            "W",
            "--max-continuations",
            "200",
            "--max-wall-seconds",
            "1000000",
            "--reason",
            "Explicit owner limits",
        )
        result = run(
            "set-policy",
            "--workstream-id",
            "W",
            "--no-wall-time-limit",
            "--expected-revision",
            "1",
            "--reason",
            "Allow provider waits",
        )
        self.assertEqual(
            result["limits"], {"max_continuations": 200, "max_wall_seconds": None}
        )
        result = run(
            "set-policy",
            "--workstream-id",
            "W",
            "--unlimited",
            "--reason",
            "Accepted plan",
        )
        self.assertEqual(
            result["limits"], {"max_continuations": None, "max_wall_seconds": None}
        )
