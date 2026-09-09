"""Deterministic allocation and native lifecycle acceptance without a database."""

from __future__ import annotations

import contextlib
import faulthandler
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import (
    cli,
    core,
    local_checks,
    operation_wait,
    platform_runtime,
    resource_checks,
    resource_cli,
    resource_queue,
    resource_runner,
    resource_service,
    worker_lease,
)
from orchestrator_engine.resource_queue import (
    OWNING,
    Ledger,
    ResourceError,
    assignments,
    compatible,
    digest,
    normalize_registry,
)
from orchestrator_engine.resource_service import (
    Authority,
    build_input_contract,
    client,
    connect,
    initialize,
    input_manifest,
    local_directory,
    prepare_input_contract,
    serve,
    submit_input_contract,
    update_configuration,
    validate_input_contract,
)


def leaf(**fields):
    return {"release": "process", **fields}


def need(name, **fields):
    return {"resource": name, **fields}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 100.0
        self.ledger = Ledger(self.root, create=True, clock=lambda: self.now)
        self.addCleanup(self.ledger.close)
        self.ledger.configure({"A": leaf(), "B": leaf(), "C": leaf()})
        self.counter = 0

    def submit(self, needs, *, stages=None, subscriber=None):
        self.counter += 1
        return self.ledger.submit(
            "project",
            str(self.counter),
            {"stages": stages or [{"id": "test", "needs": needs}]},
            subscriber,
        )

    def finish(self, stage, released=None):
        self.ledger.finish(
            stage["id"],
            stage["epoch"],
            "passed",
            list(stage["allocation"]) if released is None else released,
            {"quiescent": True},
        )

    def test_retained_input_contract_rejects_tampering_and_unsafe_paths(self):
        contract = build_input_contract(
            recipe="verify",
            recipe_digest="a" * 64,
            request_id="attempt-001",
            lineage=None,
            inputs={"input.txt": "b" * 64},
        )
        self.assertEqual(validate_input_contract(contract), contract)
        with self.assertRaisesRegex(ResourceError, "digest does not match"):
            validate_input_contract({**contract, "request_id": "changed"})
        for relative in (
            "../outside",
            "/absolute",
            "a//b",
            "..\\outside",
            ".orchestrator/x",
        ):
            with (
                self.subTest(relative=relative),
                self.assertRaisesRegex(ResourceError, "stay inside"),
            ):
                build_input_contract(
                    recipe="verify",
                    recipe_digest="a" * 64,
                    request_id="attempt-001",
                    inputs={relative: "b" * 64},
                )

    def test_rejected_token_cannot_release_another_supervisor(self):
        self.submit([need("A")])
        stage = self.ledger.schedule()[0]
        self.ledger.attach(stage["id"], stage["epoch"], {"pid": os.getpid()})
        resource_runner.execute(self.root, stage["id"], "wrong-token")
        self.assertEqual(self.ledger.stage(stage["id"])["state"], "launching")
        self.ledger.admit(stage["id"], stage["launch_token"])
        self.ledger.cancel(stage["request"])
        self.assertFalse(
            self.ledger.abort_unadmitted(
                stage["id"],
                stage["epoch"],
                token=stage["launch_token"],
            )
        )
        self.assertEqual(set(self.ledger.stage(stage["id"])["allocation"]), {"A"})

    def test_recovery_preserves_successful_dependency_and_final_outcome(self):
        request = self.submit(
            [],
            stages=[
                {"id": "first", "needs": [need("A")]},
                {"id": "second", "needs": [need("B")], "after": ["first"]},
            ],
            subscriber={"id": "listener"},
        )
        first = self.ledger.schedule()[0]
        self.finish(first, released=[])
        self.assertEqual(self.ledger.schedule(), [])
        self.assertEqual(self.ledger.stages(request)[1]["state"], "waiting")
        self.assertEqual(
            self.ledger.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1
        )
        self.ledger.recover(first["id"], first["epoch"], ["A"], {"quiescent": True})
        second = self.ledger.schedule()[0]
        self.finish(second)
        rows = self.ledger.db.execute("SELECT body FROM outbox").fetchall()

        values = [json.loads(row[0]) for row in rows]
        self.assertEqual([v["status"] for v in values], ["action_required", "passed"])
        self.assertEqual(values[0]["stages"][0]["state"], "recovery_required")
        self.ledger.schedule()
        self.assertEqual(
            self.ledger.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 2
        )

    def test_sequential_recovery_incidents_have_distinct_outbox_identity(self):
        self.submit(
            [],
            stages=[
                {"id": "first", "needs": [need("A")]},
                {"id": "second", "needs": [need("B")], "after": ["first"]},
            ],
            subscriber={"id": "listener"},
        )
        first = self.ledger.schedule()[0]
        self.finish(first, released=[])
        self.ledger.recover(first["id"], first["epoch"], ["A"], {"quiescent": True})
        second = self.ledger.schedule()[0]
        self.finish(second, released=[])
        values = [
            json.loads(row[0])
            for row in self.ledger.db.execute("SELECT body FROM outbox")
        ]
        self.assertEqual(len(values), 2)
        self.assertNotEqual(values[0]["outcome_id"], values[1]["outcome_id"])
        self.assertEqual({v["status"] for v in values}, {"action_required"})

    def test_invalid_subscriber_is_rejected_before_execution(self):
        request = self.submit([])
        contract = digest(self.ledger.request(request)["plan"])
        for subscriber in (
            {"id": "listener", "check_id": []},
            {"id": "listener", "wake_target": "bad"},
            {"id": "listener", "wake": "false"},
            {"id": "listener", "wake_policy": []},
            {"id": "listener", "unsupported": True},
        ):
            with self.subTest(subscriber=subscriber):
                with self.assertRaises(core.OrchestratorError):
                    self.submit([], subscriber=subscriber)
                with self.assertRaises(core.OrchestratorError):
                    self.ledger.subscribe(request, contract, subscriber)
        self.assertEqual(self.ledger.request(request)["subscribers"], [])

    def test_assignment_depth_does_not_inherit_python_recursion_limit(self):
        registry = normalize_registry({"A": leaf(capacity=1500)})
        result = next(
            assignments(
                registry,
                [need("A", mode="shared", compatibility="reader") for _ in range(1500)],
            )
        )
        self.assertEqual(result["A"]["units"], 1500)

    def test_pool_search_does_not_materialize_cartesian_product(self):
        registry = {}
        for index in range(5):
            members = [f"leaf-{index}-{j}" for j in range(10)]
            registry.update({member: leaf() for member in members})
            registry[f"pool-{index}"] = {"kind": "pool", "members": members}
        self.ledger.configure(registry)
        self.submit([need(f"pool-{i}") for i in range(5)])
        yielded = 0
        original = assignments

        def bounded(*args, **kwargs):
            nonlocal yielded
            for allocation in original(*args, **kwargs):
                yielded += 1
                self.assertLess(
                    yielded, 20, "scheduler eagerly enumerated pool combinations"
                )
                yield allocation

        with mock.patch.object(resource_queue, "assignments", bounded):
            self.assertEqual(len(self.ledger.schedule()), 1)

    def test_search_prunes_blocked_prefix_before_expanding_other_pools(self):
        registry = normalize_registry(
            {
                "A": leaf(),
                "B": leaf(),
                "pool": {"kind": "pool", "members": ["A", "B"]},
            }
        )
        occupied = {"A": {"mode": "exclusive", "units": 1, "compatibility": None}}
        with mock.patch.object(resource_queue, "compatible", wraps=compatible) as check:
            self.assertEqual(
                list(
                    assignments(
                        registry,
                        [need("A")] + [need("pool")] * 15,
                        others=[occupied],
                    )
                ),
                [],
            )
        self.assertEqual(check.call_count, 1)

    def test_disjoint_work_bypasses_blocked_multi_resource_request(self):
        self.submit([need("A")])
        first = self.ledger.schedule()[0]
        blocked = self.submit([need("A"), need("B")])
        useful = self.submit([need("B")])
        independent = self.submit([need("C")])
        grants = self.ledger.schedule()
        self.assertEqual({g["request"] for g in grants}, {useful, independent})
        self.assertEqual(self.ledger.stages(blocked)[0]["allocation"], {})
        self.finish(first)

    def test_release_protection_drains_only_conflicting_set(self):
        self.submit([need("A")])
        self.ledger.schedule()
        older = self.submit([need("A"), need("B")])
        self.submit([need("B")])
        younger = self.ledger.schedule()[0]
        self.finish(younger)
        blocked = self.submit([need("B")])
        disjoint = self.submit([need("C")])
        grants = self.ledger.schedule()
        self.assertEqual([g["request"] for g in grants], [disjoint])
        self.assertIn("protection", self.ledger.stages(older)[0])
        self.assertEqual(self.ledger.stages(blocked)[0]["state"], "waiting")
        self.ledger.cancel(older)
        self.assertEqual(self.ledger.schedule()[0]["request"], blocked)

    def test_writer_drain_blocks_new_readers_after_release(self):
        self.ledger.configure({"A": leaf(capacity=3)})
        read = [need("A", mode="shared", compatibility="read")]
        self.submit(read)
        self.submit(read)
        readers = self.ledger.schedule()
        writer = self.submit([need("A")])
        self.ledger.schedule()
        self.finish(readers[0])
        self.submit(read)
        self.assertEqual(self.ledger.schedule(), [])
        self.finish(readers[1])
        self.assertEqual(self.ledger.schedule()[0]["request"], writer)

    def test_pool_backtracks_and_preserves_scarce_member(self):
        self.ledger.configure(
            {"A": leaf(), "B": leaf(), "pool": {"kind": "pool", "members": ["A", "B"]}}
        )
        flexible = self.submit([need("pool")])
        specific = self.submit([need("A")])
        grants = self.ledger.schedule()
        self.assertEqual({g["request"] for g in grants}, {flexible, specific})
        self.assertEqual(set(grants[0]["allocation"]), {"B"})
        options = list(
            assignments(self.ledger.get("registry"), [need("pool"), need("A")])
        )
        self.assertEqual(len(options), 1)

    def test_alias_and_bundle_cannot_double_grant(self):
        self.ledger.configure(
            {
                "A": leaf(),
                "B": leaf(),
                "alias": {"kind": "alias", "members": ["A"]},
                "bundle": {"kind": "bundle", "members": ["A", "B"]},
            }
        )
        self.submit([need("alias")])
        self.submit([need("bundle")])
        self.assertEqual(len(self.ledger.schedule()), 1)
        with self.assertRaises(ResourceError):
            self.submit([need("A"), need("alias")])

    def test_bundle_backtracks_past_overlapping_pool_member(self):
        registry = normalize_registry(
            {
                "A": leaf(),
                "B": leaf(),
                "pool": {"kind": "pool", "members": ["A", "B"]},
                "bundle": {"kind": "bundle", "members": ["pool", "A"]},
            }
        )
        choices = list(assignments(registry, [need("bundle")]))
        self.assertEqual([set(a) for a in choices], [{"A", "B"}])

    def test_different_projects_share_one_physical_capacity(self):
        self.submit([need("A")])
        other = self.ledger.submit(
            "other", "1", {"stages": [{"id": "verify", "needs": [need("A")]}]}
        )
        stage = self.ledger.schedule()[0]
        self.assertEqual(self.ledger.stages(other)[0]["state"], "waiting")
        self.finish(stage)
        self.assertEqual(self.ledger.schedule()[0]["request"], other)

    def test_shared_capacity_and_compatibility_are_both_required(self):
        self.ledger.configure({"A": leaf(capacity=3)})
        self.submit([need("A", mode="shared", units=2, compatibility="read")])
        self.submit([need("A", mode="shared", units=1, compatibility="other")])
        self.submit([need("A", mode="shared", units=2, compatibility="read")])
        self.submit([need("A", mode="shared", units=1, compatibility="read")])
        self.assertEqual(len(self.ledger.schedule()), 2)

    def test_ready_order_is_not_submission_order(self):
        earlier = self.submit(
            [],
            stages=[
                {"id": "prepare", "needs": []},
                {"id": "protected", "after": ["prepare"], "needs": [need("A")]},
            ],
        )
        preparing = self.ledger.schedule()[0]
        self.submit([need("A")])
        occupying = self.ledger.schedule()[0]
        ready = self.submit([need("A")])
        self.ledger.schedule()
        self.finish(preparing)
        self.ledger.schedule()
        self.finish(occupying)
        self.assertEqual(self.ledger.schedule()[0]["request"], ready)
        self.assertEqual(self.ledger.stages(earlier)[1]["state"], "waiting")

    def test_host_wait_does_not_protect_or_acquire(self):
        request = self.submit([need("A")])
        self.assertEqual(self.ledger.schedule(["project"]), [])
        self.assertEqual(self.ledger.stages(request)[0]["reason"], "host_wait")
        self.assertEqual(self.ledger.schedule()[0]["request"], request)

    def test_admission_is_single_use_and_cancel_is_ordered(self):
        request = self.submit([need("A")])
        stage = self.ledger.schedule()[0]
        self.ledger.attach(stage["id"], stage["epoch"], {"pid": 123})
        self.ledger.cancel(request)
        with self.assertRaises(ResourceError):
            self.ledger.admit(stage["id"], stage["launch_token"])
        self.finish(stage)
        self.submit([need("A")])
        stage = self.ledger.schedule()[0]
        self.ledger.attach(stage["id"], stage["epoch"], {"pid": 123})
        self.ledger.admit(stage["id"], stage["launch_token"])
        with self.assertRaises(ResourceError):
            self.ledger.admit(stage["id"], stage["launch_token"])

    def test_partial_release_and_stale_recovery(self):
        self.submit([need("A"), need("B")])
        stage = self.ledger.schedule()[0]
        self.finish(stage, ["B"])
        self.submit([need("A")])
        available = self.submit([need("B")])
        self.assertEqual([s["request"] for s in self.ledger.schedule()], [available])
        with self.assertRaises(ResourceError):
            self.ledger.recover(stage["id"], 999, ["A"], {"quiescent": True})
        self.ledger.recover(stage["id"], stage["epoch"], ["A"], {"quiescent": True})
        self.assertEqual(len(self.ledger.schedule()), 1)

    def test_restart_preserves_release_trigger_and_idempotency(self):
        request = self.submit([need("A")])
        stage = self.ledger.schedule()[0]
        self.finish(stage)
        with contextlib.closing(Ledger(self.root)) as other:
            self.assertEqual(other.get("released"), ["A"])
            self.assertEqual(
                other.submit(
                    "project", "1", {"stages": [{"id": "test", "needs": [need("A")]}]}
                ),
                request,
            )
            with self.assertRaises(ResourceError):
                other.submit("project", "1", {"stages": [{"id": "changed"}]})

    def test_failed_dependency_and_subscriber_outbox(self):
        subscriber = {"id": "listener"}
        request = self.submit(
            [],
            stages=[{"id": "a", "needs": [need("A")]}, {"id": "b", "after": ["a"]}],
            subscriber=subscriber,
        )
        stage = self.ledger.schedule()[0]
        self.ledger.finish(stage["id"], 1, "failed", [], {"uncertain": True})
        self.ledger.schedule()
        self.assertEqual(self.ledger.stages(request)[1]["state"], "skipped")
        self.assertEqual(
            self.ledger.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1
        )
        self.ledger.schedule()
        self.assertEqual(
            self.ledger.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1
        )

    def test_subscriber_cancel_does_not_cancel_execution(self):
        request = self.submit([need("A")], subscriber={"id": "one"})
        contract = digest(self.ledger.request(request)["plan"])
        self.ledger.subscribe(request, contract, {"id": "two"})
        self.ledger.subscribe(request, contract, {"id": "one"}, remove=True)
        self.assertEqual(len(self.ledger.schedule()), 1)
        self.assertEqual(self.ledger.request(request)["subscribers"], [{"id": "two"}])

    def test_configuration_change_cannot_hide_existing_ownership(self):
        self.submit([need("A")])
        self.ledger.schedule()
        with self.assertRaises(ResourceError):
            self.ledger.configure({"A": leaf(incarnation="2")})
        with self.assertRaises(ResourceError):
            normalize_registry(
                {"A": leaf(physical_id="same"), "B": leaf(physical_id="same")}
            )

    def test_metrics_separate_wait_ownership_and_overlapping_capacity(self):
        self.ledger.configure({"A": leaf(capacity=2)})
        self.submit([need("A", mode="shared", compatibility="read")])
        self.submit([need("A", mode="shared", compatibility="read")])
        stages = self.ledger.schedule()
        self.now += 4
        for stage in stages:
            self.finish(stage)
        metrics = self.ledger.metrics("project")
        self.assertEqual(metrics["resources"]["A"]["owned_wall_seconds"], 4)
        self.assertEqual(metrics["resources"]["A"]["owned_capacity_unit_seconds"], 8)
        self.assertIsNone(metrics["agent_active_seconds"])
        self.assertIsNone(metrics["eligible_idle_seconds"])

    def test_two_connections_cannot_double_admit(self):
        for _ in range(12):
            self.submit([need("A")])
        errors = []

        def schedule():
            try:
                with contextlib.closing(Ledger(self.root)) as ledger:
                    ledger.schedule()
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=schedule) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        active = [s for s in self.ledger.stages() if s["state"] in OWNING]
        self.assertEqual(len(active), 1)
        self.assertTrue(
            compatible(self.ledger.get("registry"), active[0]["allocation"], [])
        )


@unittest.skipUnless(
    platform_runtime.detached_lifecycle_supported(), "native runtime required"
)
class ResourceNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "authority"
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "input.txt").write_text("pinned")
        self.config = {
            "resources": {"db": leaf()},
            "projects": {
                "sample": {
                    "root": str(self.project),
                    "recipes": {
                        "verify": {
                            "inputs": ["input.txt"],
                            "stages": [
                                {
                                    "id": "verify",
                                    "needs": [need("db")],
                                    "commands": [
                                        {
                                            "argv": [
                                                "{python}",
                                                "-c",
                                                "print('verified')",
                                            ]
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                }
            },
        }
        initialize(self.directory, self.config)

    @contextlib.contextmanager
    def service(self):
        ready, stop = threading.Event(), threading.Event()
        errors = []

        def run():
            try:
                serve(self.directory, ready=ready, stop=stop)
            except Exception as error:
                errors.append(error)
                ready.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            started = ready.wait(5)
            if not started:
                faulthandler.dump_traceback()
            self.assertTrue(started, "service startup deadline")
            self.assertEqual(errors, [])
            connect(self.directory, "sample", self.project)
            yield core.load_object(self.project / ".orchestrator" / "resources.json")
        finally:
            stop.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def await_terminal(self, connection, request):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            report = client(connection, "status", {"request": request})
            if report["terminal"] or report["action_required"]:
                return report
            time.sleep(0.05)
        self.fail("resource attempt did not terminate within fixture deadline")

    @unittest.skipUnless(sys.platform == "linux", "Linux mount classification")
    def test_authority_checks_resolved_symlink_mount(self):
        foreign = Path(self.temp.name) / "foreign"
        foreign.mkdir()
        link = self.project / "authority-link"
        link.symlink_to(foreign, target_is_directory=True)
        original = Path.read_text

        def read(path, *args, **kwargs):
            if str(path) == "/proc/mounts":
                return f"root / ext4 rw 0 0\nremote {foreign} nfs rw 0 0\n"
            return original(path, *args, **kwargs)

        with (
            mock.patch.object(Path, "read_text", read),
            self.assertRaisesRegex(ResourceError, "shared/foreign"),
        ):
            local_directory(link / "ledger")

    def test_client_rejects_stale_identity_before_building_transport(self):
        connection = {
            "url": "http://127.0.0.1:12345",
            "authority": "test-authority",
            "project": "sample",
            "token": "private-token",
            "identity": {"pid": 123, "start_ticks": 456},
        }
        with (
            mock.patch.object(
                resource_service.worker_lease,
                "identity_state",
                return_value={"state": "gone", "identity_verified": False},
            ),
            mock.patch.object(
                resource_service.urllib.request, "build_opener"
            ) as build_opener,
            self.assertRaisesRegex(ResourceError, "identity.*stale"),
        ):
            client(connection, "status", {})
        build_opener.assert_not_called()

    def test_service_refuses_to_publish_without_process_identity(self):
        with (
            mock.patch.object(
                resource_service.worker_lease,
                "process_identity",
                return_value=None,
            ),
            self.assertRaisesRegex(ResourceError, "process identity is unavailable"),
        ):
            serve(self.directory)

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_authority_requires_private_directory_and_ledger(self):
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.directory / "resources.sqlite3").stat().st_mode),
            0o600,
        )
        insecure = Path(self.temp.name) / "insecure-authority"
        insecure.mkdir(mode=0o755)
        insecure.chmod(0o755)
        with self.assertRaisesRegex(ResourceError, "mode 0700"):
            initialize(insecure, self.config)

    def test_loopback_service_startup_does_not_resolve_host_names(self):
        with (
            mock.patch("socket.getfqdn", side_effect=AssertionError("DNS unavailable")),
            self.service() as connection,
        ):
            self.assertEqual(client(connection, "status", {})["stages"], [])

    def test_http_registered_recipe_and_authority_restart(self):
        with self.service() as connection:
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(
                        (
                            self.project / ".orchestrator" / "resources.json"
                        ).stat().st_mode
                    ),
                    0o600,
                )
            result = client(connection, "submit", self.payload())
            report = self.await_terminal(connection, result["request"])
            self.assertEqual(report["stages"][0]["state"], "passed")
            old_url = connection["url"]
            with self.assertRaises(ResourceError):
                client({**connection, "token": "bad"}, "status", {})
            saved_connection = connection
        with self.service() as connection:
            self.assertEqual(connection["url"], old_url)
            replay = client(saved_connection, "submit", self.payload())
            self.assertEqual(replay["request"], result["request"])

    def test_first_class_check_routes_to_resource_authority(self):
        with self.service():
            (self.project / ".orchestrator" / "checks.toml").write_text(
                '[suites.verify]\nresource_recipe = "verify"\n'
                'verification = "focused"\n'
            )
            check = local_checks.start_check(
                self.project,
                check_id="queued-check",
                suite="verify",
                wake_policy="never",
            )
            self.assertTrue(check["resource_managed"])
            operation_wait.wait_for_operations(
                self.project,
                targets=["check:queued-check"],
                timeout_seconds=15,
                interval_seconds=0.05,
            )
            status = local_checks.check_status(self.project, check_id="queued-check")
            self.assertEqual(status["checks"][0]["status"], "passed")
            again = local_checks.start_check(
                self.project,
                check_id="queued-check",
                suite="verify",
                wake_policy="never",
            )
            self.assertEqual(again["resource_request"], check["resource_request"])

    def test_invalid_duration_policy_does_not_claim_a_check(self):
        state = self.project / ".orchestrator"
        state.mkdir()
        (state / "checks.toml").write_text(
            '[suites.verify]\nresource_recipe = "verify"\n'
        )
        for threshold in (-1, 0, float("inf"), float("nan")):
            with self.subTest(threshold=threshold), self.assertRaises(ResourceError):
                local_checks.start_check(
                    self.project,
                    check_id="invalid-policy",
                    suite="verify",
                    wake_policy="never",
                    long_threshold_seconds=threshold,
                )
        self.assertFalse((state / "checks" / "invalid-policy").exists())

    def test_authority_replacement_does_not_duplicate_live_execution(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        authority.tick()
        replacement = Authority(self.directory)
        replacement.tick()
        self.assertEqual(replacement.children, {})
        process = next(iter(authority.children.values()))
        self.assertEqual(process.wait(timeout=15), 0)
        replacement.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(ledger.stages(request)[0]["state"], "passed")

    def test_completion_racing_reconciliation_does_not_stop_authority(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.schedule()[0]

            def completed(_identity):
                ledger.finish(stage["id"], stage["epoch"], "passed", ["db"], {})
                return {"state": "gone"}

            with mock.patch(
                "orchestrator_engine.worker_lease.identity_state", side_effect=completed
            ):
                authority.reconcile(ledger)
            self.assertEqual(ledger.stages(request)[0]["state"], "passed")

    def test_running_cancellation_waits_for_native_cleanup(self):
        self.directory = self.directory / "cancel-authority"
        recipe = self.config["projects"]["sample"]["recipes"]["verify"]
        recipe["stages"][0]["commands"] = [
            {"argv": ["{python}", "-c", "import time; time.sleep(30)"]}
        ]
        initialize(self.directory, self.config)
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        authority.tick()
        process = next(iter(authority.children.values()))
        with contextlib.closing(Ledger(self.directory)) as ledger:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ledger.stages(request)[0].get("command_identity"):
                    break
                time.sleep(0.05)
            self.assertIsNotNone(ledger.stages(request)[0].get("command_identity"))
            ledger.cancel(request)
        process.wait(timeout=15)
        authority.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(ledger.stages(request)[0]["state"], "cancelled")
            self.assertEqual(ledger.stages(request)[0]["allocation"], {})

    def test_cancelled_runner_cleanup_and_probe_use_maintenance_context(self):
        self.directory = self.directory / "maintenance-authority"
        context_check = (
            "import json, os; from pathlib import Path; "
            "from orchestrator_engine import core; "
            "from orchestrator_engine.resource_service import client; "
            "connection=core.load_object(Path(os.environ["
            "'ORCHESTRATOR_RESOURCE_CONNECTION'])); "
            "context=json.loads(os.environ['ORCHESTRATOR_RESOURCE_CONTEXT']); "
            "result=client(connection, 'context', context); "
            "assert result['valid'] and result['purpose']=='maintenance'"
        )
        stage = self.config["projects"]["sample"]["recipes"]["verify"]["stages"][0]
        stage["commands"] = [
            {"argv": ["{python}", "-c", "import time; time.sleep(30)"]}
        ]
        stage["cleanup"] = [{"argv": ["{python}", "-c", context_check]}]
        self.config["resources"]["db"] = {
            "release": "probe",
            "probe": {
                "argv": ["{python}", "-c", context_check],
                "timeout_seconds": 5,
            },
        }
        initialize(self.directory, self.config)

        with self.service() as connection:
            request = client(connection, "submit", self.payload())["request"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with contextlib.closing(Ledger(self.directory)) as ledger:
                    if ledger.stages(request)[0].get("command_identity"):
                        break
                time.sleep(0.05)
            else:
                self.fail("resource command did not start")
            client(connection, "cancel", {"request": request})
            report = self.await_terminal(connection, request)

        self.assertTrue(report["terminal"])
        self.assertFalse(report["action_required"])
        self.assertEqual(report["stages"][0]["state"], "cancelled")
        evidence = core.load_object(Path(report["stages"][0]["evidence"]["path"]))
        self.assertEqual(evidence["results"][-1]["exit_code"], 0)
        self.assertEqual(evidence["probes"]["db"]["exit_code"], 0)

    def test_cancelled_owner_retains_only_phase_scoped_maintenance_context(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.schedule()[0]
            ledger.attach(
                stage["id"],
                stage["epoch"],
                worker_lease.process_identity(os.getpid()),
            )
            stage = ledger.admit(stage["id"], stage["launch_token"])
        common = {
            "request": request,
            "stage": stage["id"],
            "epoch": stage["epoch"],
        }
        work = {**common, "purpose": "work", "token": stage["launch_token"]}
        maintenance = {
            **common,
            "purpose": "maintenance",
            "token": stage["maintenance_token"],
        }
        self.assertEqual(authority.call("sample", "context", work)["purpose"], "work")
        with contextlib.closing(Ledger(self.directory)) as ledger:
            ledger.cancel(request)
        with self.assertRaisesRegex(ResourceError, "revoked"):
            authority.call("sample", "context", work)
        with self.assertRaisesRegex(ResourceError, "revoked"):
            authority.call(
                "sample",
                "context",
                {**common, "purpose": "maintenance", "token": ""},
            )
        self.assertEqual(
            authority.call("sample", "context", maintenance)["purpose"],
            "maintenance",
        )
        with self.assertRaisesRegex(ResourceError, "revoked"):
            authority.call(
                "sample",
                "context",
                {**work, "token": stage["maintenance_token"]},
            )
        with contextlib.closing(Ledger(self.directory)) as ledger:
            ledger.finish(
                stage["id"], stage["epoch"], "cancelled", ["db"], {"ok": True}
            )
        with self.assertRaisesRegex(ResourceError, "revoked"):
            authority.call("sample", "context", maintenance)

    def test_legacy_stage_without_maintenance_capability_fails_closed(self):
        with self.assertRaisesRegex(ResourceError, "drain before upgrade"):
            resource_runner.phase_capability(
                {"launch_token": "legacy-work-token"}, maintenance=True
            )

    def test_legacy_running_stage_cannot_authenticate_empty_maintenance_token(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.schedule()[0]
            ledger.attach(
                stage["id"],
                stage["epoch"],
                worker_lease.process_identity(os.getpid()),
            )
            stage = ledger.admit(stage["id"], stage["launch_token"])
            stage.pop("maintenance_token")
            ledger.save(stage)
        payload = {
            "request": request,
            "stage": stage["id"],
            "epoch": stage["epoch"],
            "purpose": "maintenance",
        }

        for supplied in (None, ""):
            candidate = dict(payload)
            if supplied is not None:
                candidate["token"] = supplied
            with self.subTest(token=supplied), self.assertRaisesRegex(
                ResourceError, "revoked"
            ):
                authority.call("sample", "context", candidate)

    def test_private_stage_capabilities_never_enter_public_status(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.schedule()[0]
            snapshot = ledger.snapshot("sample", request)
        self.assertIn("launch_token", stage)
        self.assertIn("maintenance_token", stage)
        self.assertNotIn("launch_token", snapshot["stages"][0])
        self.assertNotIn("maintenance_token", snapshot["stages"][0])

    def test_configuration_update_is_revisioned_drained_and_preserves_identity(self):
        before = core.load_object(self.directory / "config.json")
        updated = json.loads(json.dumps(self.config))
        updated["projects"]["sample"]["recipes"]["verify"]["stages"][0][
            "commands"
        ][0]["argv"] = ["{python}", "-c", "print('updated')"]
        result = update_configuration(
            self.directory, updated, expected_revision=before["revision"]
        )
        after = core.load_object(self.directory / "config.json")
        self.assertEqual(result["revision"], before["revision"] + 1)
        self.assertEqual(after["authority"], before["authority"])
        self.assertEqual(
            after["projects"]["sample"]["token"],
            before["projects"]["sample"]["token"],
        )
        Authority(self.directory)
        with self.assertRaisesRegex(ResourceError, "revision changed"):
            update_configuration(
                self.directory, updated, expected_revision=before["revision"]
            )

    def test_configuration_update_refuses_live_or_waiting_work(self):
        authority = Authority(self.directory)
        authority.submit("sample", self.payload())
        with self.assertRaisesRegex(ResourceError, "fully drained"):
            update_configuration(
                self.directory, self.config, expected_revision=1
            )

    def test_configuration_update_rejects_inconsistent_ledger(self):
        with contextlib.closing(Ledger(self.directory)) as ledger:
            ledger.configure({"changed": leaf()})
        with self.assertRaisesRegex(ResourceError, "does not match"):
            update_configuration(
                self.directory, self.config, expected_revision=1
            )

    def test_missing_runner_evidence_blocks_delivery_but_not_capacity(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        authority.tick()
        next(iter(authority.children.values())).wait(timeout=15)
        with contextlib.closing(Ledger(self.directory)) as ledger:
            path = Path(ledger.stages(request)[0]["evidence"]["path"])
            path.unlink()
        authority.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            snapshot = ledger.snapshot("sample", request)
            self.assertEqual(snapshot["stages"][0]["allocation"], {})
            self.assertTrue(snapshot["action_required"])
            self.assertEqual(snapshot["delivery"][0]["delivered"], 0)

    def test_probe_failure_quarantines_resource(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            registry = ledger.get("registry")
            registry["db"].update(
                release="probe",
                probe={
                    "argv": ["{python}", "-c", "raise SystemExit(1)"],
                    "timeout_seconds": 2,
                },
            )
            ledger.put("registry", registry)
        authority.tick()
        next(iter(authority.children.values())).wait(timeout=15)
        authority.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(ledger.stages(request)[0]["state"], "recovery_required")

    def test_cancel_before_admission_runs_no_protected_command(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.schedule()[0]
            ledger.attach(stage["id"], stage["epoch"], {"pid": os.getpid()})
            ledger.cancel(request)
        resource_runner.execute(self.directory, stage["id"], stage["launch_token"])
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(ledger.stage(stage["id"])["state"], "cancelled")

    def payload(self, request_id="one"):
        recipe = self.config["projects"]["sample"]["recipes"]["verify"]
        return {
            "id": request_id,
            "recipe": "verify",
            "recipe_digest": digest(recipe),
            "inputs": input_manifest(self.project, recipe["inputs"]),
            "subscriber": {"id": request_id, "wake": False},
        }

    def test_check_recovery_has_separate_immutable_advisory_and_final_result(self):
        authority = Authority(self.directory)
        state = self.project / ".orchestrator"
        state.mkdir()
        core.atomic_json(state / "resources.json", {})
        (state / "checks.toml").write_text(
            '[suites.verify]\nresource_recipe = "verify"\nverification = "focused"\n'
        )
        with mock.patch.object(
            resource_checks,
            "client",
            side_effect=lambda c, a, p: authority.call("sample", a, p),
        ):
            check = local_checks.start_check(
                self.project,
                check_id="recovery-check",
                suite="verify",
                wake_policy="never",
            )
            with contextlib.closing(Ledger(self.directory)) as ledger:
                stage = ledger.schedule()[0]
                ledger.finish(
                    stage["id"], stage["epoch"], "passed", [], {"quiescent": False}
                )
                authority.deliver(ledger)
                events = [
                    core.event_path_for(self.project, row[0])
                    for row in ledger.db.execute(
                        "SELECT id FROM outbox WHERE delivered=1"
                    )
                ]
                self.assertEqual(len(events), 1)
                advisory_files = list(
                    (state / "checks" / "recovery-check").glob("*required*.json")
                )
                self.assertEqual(len(advisory_files), 1)
                advisory_digest = core.sha256_file(advisory_files[0])
                status = local_checks.check_status(
                    self.project, check_id="recovery-check"
                )["checks"][0]
                self.assertEqual(status["status"], "stalled")
                self.assertFalse(
                    (advisory_files[0].parent / "verification-result.json").exists()
                )
                ledger.recover(stage["id"], stage["epoch"], ["db"], {"quiescent": True})
                authority.deliver(ledger)
                status = local_checks.check_status(
                    self.project, check_id="recovery-check"
                )["checks"][0]
                self.assertEqual(status["status"], "passed", status)
                self.assertEqual(core.sha256_file(advisory_files[0]), advisory_digest)
                for event in events:
                    core.verify_terminal_event(event)
                self.assertEqual(
                    ledger.db.execute(
                        "SELECT count(*) FROM outbox WHERE delivered=1"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(check["resource_request"], stage["request"])

    def test_malformed_projection_does_not_stop_other_delivery(self):
        authority = Authority(self.directory)
        payload = self.payload()
        payload["subscriber"]["check_id"] = "bad-projection"
        first = authority.submit("sample", payload)["request"]
        second = authority.submit("sample", self.payload("second"))["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            for _ in range(2):
                stage = ledger.schedule()[0]
                ledger.finish(stage["id"], stage["epoch"], "passed", ["db"], {})
            with mock.patch.object(
                resource_checks,
                "project_result",
                side_effect=ValueError("malformed saved descriptor"),
            ):
                authority.deliver(ledger)
            outcomes = {
                row["request"]: dict(row)
                for row in ledger.db.execute("SELECT * FROM outbox")
            }
            self.assertEqual(outcomes[first]["delivered"], 0)
            self.assertEqual(outcomes[first]["attempts"], 1)
            self.assertEqual(outcomes[second]["delivered"], 1)
            self.assertTrue(ledger.snapshot("sample", first)["action_required"])

    def test_recovery_before_delivery_supersedes_stale_advisory(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.schedule()[0]
            ledger.finish(stage["id"], stage["epoch"], "passed", [], {})
            ledger.recover(stage["id"], stage["epoch"], ["db"], {"quiescent": True})
            authority.deliver(ledger)
            result = (
                self.project / ".orchestrator" / "resources" / request / "result.json"
            )
            self.assertEqual(core.load_object(result)["status"], "passed")
            self.assertFalse((result.parent / "recovery-required.json").exists())

    def test_native_execution_and_durable_delivery(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        authority.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.stages(request)[0]
        process = authority.children[stage["id"]]
        self.assertEqual(process.wait(timeout=15), 0)
        authority.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            result = ledger.snapshot("sample", request)
            self.assertEqual(result["stages"][0]["state"], "passed", result)
            self.assertEqual(
                ledger.db.execute("SELECT delivered FROM outbox").fetchone()[0], 1
            )
        output = self.project / ".orchestrator" / "resources" / request / "result.json"
        self.assertTrue(output.exists())

    def test_snapshot_replay_and_drift_rejection(self):
        authority = Authority(self.directory)
        payload = self.payload()
        first = authority.submit("sample", payload)
        (self.project / "input.txt").write_text("changed")
        self.assertEqual(
            authority.submit("sample", payload)["request"], first["request"]
        )
        with self.assertRaises(ResourceError):
            authority.submit("sample", {**payload, "id": "other"})
        with self.assertRaises(ResourceError):
            authority.submit("sample", self.payload())

    def test_delayed_retained_contract_rejects_replacement_and_replays_offline(self):
        with self.service() as connection:
            retained = prepare_input_contract(
                self.project,
                recipe="verify",
                request_id="delayed-attempt",
                lineage="prior-attempt",
            )
            (self.project / "input.txt").write_text("replacement")
            replacement = prepare_input_contract(
                self.project,
                recipe="verify",
                request_id="delayed-attempt",
                lineage="prior-attempt",
            )
            with self.assertRaisesRegex(ResourceError, "declared input snapshot"):
                submit_input_contract(
                    self.project,
                    input_contract=retained,
                    subscriber={"id": "delayed-attempt", "wake": False},
                )

            (self.project / "input.txt").write_text("pinned")
            first = submit_input_contract(
                self.project,
                input_contract=retained,
                subscriber={"id": "delayed-attempt", "wake": False},
            )
            (self.project / "input.txt").unlink()
            replay = submit_input_contract(
                self.project,
                input_contract=retained,
                subscriber={"id": "delayed-attempt", "wake": False},
            )
            self.assertEqual(replay["request"], first["request"])
            self.assertTrue(replay["idempotent"])

            (self.project / "input.txt").write_text("replacement")
            with self.assertRaisesRegex(ResourceError, "different immutable inputs"):
                submit_input_contract(
                    self.project,
                    input_contract=replacement,
                    subscriber={"id": "delayed-attempt", "wake": False},
                )
            with contextlib.closing(
                Ledger(connection["authority_directory"])
            ) as ledger:
                self.assertEqual(
                    ledger.db.execute("SELECT count(*) FROM requests").fetchone()[0],
                    1,
                )

    def test_resource_cli_creates_and_submits_retained_input_contract(self):
        output = self.project / "retained-inputs.json"
        with self.service():
            create_args = cli.build_parser().parse_args(
                [
                    "--project-root",
                    str(self.project),
                    "resource",
                    "create-input-contract",
                    "--recipe",
                    "verify",
                    "--id",
                    "cli-attempt",
                    "--output",
                    str(output),
                ]
            )
            created = resource_cli.run(create_args, self.project)
            self.assertEqual(created["input_contract"], str(output.resolve()))
            with self.assertRaisesRegex(ResourceError, "refusing to replace"):
                resource_cli.run(create_args, self.project)

            submit_args = cli.build_parser().parse_args(
                [
                    "--project-root",
                    str(self.project),
                    "resource",
                    "submit",
                    "--input-contract",
                    str(output),
                ]
            )
            submitted = resource_cli.run(submit_args, self.project)
            self.assertFalse(submitted["idempotent"])
            conflicting_args = cli.build_parser().parse_args(
                [
                    "--project-root",
                    str(self.project),
                    "resource",
                    "submit",
                    "--input-contract",
                    str(output),
                    "--id",
                    "different",
                ]
            )
            with self.assertRaisesRegex(ResourceError, "cannot be combined"):
                resource_cli.run(conflicting_args, self.project)

    def test_replay_adds_new_subscriber_and_rejects_changed_destination(self):
        authority = Authority(self.directory)
        payload = self.payload()
        request = authority.submit("sample", payload)["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            events_before = ledger.db.execute("SELECT count(*) FROM events").fetchone()[
                0
            ]
        self.assertTrue(authority.submit("sample", payload)["idempotent"])
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(
                ledger.db.execute("SELECT count(*) FROM events").fetchone()[0],
                events_before,
            )
            stage = ledger.schedule()[0]
            ledger.finish(stage["id"], stage["epoch"], "passed", ["db"], {})
            self.assertEqual(
                ledger.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1
            )
        second = {**payload, "subscriber": {"id": "second", "wake": True}}
        replay = authority.submit("sample", second)
        self.assertTrue(replay["idempotent"])
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(
                ledger.request(request)["subscribers"],
                [payload["subscriber"], second["subscriber"]],
            )
            self.assertEqual(
                ledger.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 2
            )
        conflicting = {**payload, "subscriber": {"id": "second", "wake": False}}
        with self.assertRaisesRegex(ResourceError, "pinned destination"):
            authority.submit("sample", conflicting)

    def test_concurrent_replay_removes_unreferenced_snapshot(self):
        authority = Authority(self.directory)
        payload = self.payload()
        barrier = threading.Barrier(2)
        original_capture = resource_service.capture
        results = []
        errors = []

        def synchronized_capture(*args):
            original_capture(*args)
            barrier.wait(timeout=5)

        def submit():
            try:
                results.append(authority.submit("sample", payload))
            except Exception as error:
                errors.append(error)

        with mock.patch.object(
            resource_service, "capture", side_effect=synchronized_capture
        ):
            threads = [threading.Thread(target=submit) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {result["request"] for result in results}, {results[0]["request"]}
        )
        self.assertEqual(len(list((self.directory / "snapshots").iterdir())), 1)

    def test_committed_snapshot_survives_ledger_close_failure(self):
        authority = Authority(self.directory)
        payload = self.payload()
        submitted = False
        original_submit = Ledger.submit
        original_close = Ledger.close

        def submit(ledger, *args, **kwargs):
            nonlocal submitted
            request = original_submit(ledger, *args, **kwargs)
            submitted = True
            return request

        def close(ledger):
            original_close(ledger)
            if submitted:
                raise OSError("synthetic close failure")

        with (
            mock.patch.object(Ledger, "submit", submit),
            mock.patch.object(Ledger, "close", close),
            self.assertRaisesRegex(OSError, "synthetic close failure"),
        ):
            authority.submit("sample", payload)
        with contextlib.closing(Ledger(self.directory)) as ledger:
            row = ledger.db.execute(
                "SELECT id FROM requests WHERE project=? AND external=?",
                ("sample", payload["id"]),
            ).fetchone()
            workspace = Path(ledger.request(row[0])["plan"]["workspace"])
        self.assertTrue(workspace.is_dir())

    def test_commit_uncertainty_preserves_referenced_snapshot(self):
        authority = Authority(self.directory)
        payload = self.payload()
        original_submit = Ledger.submit

        def uncertain_submit(ledger, *args, **kwargs):
            original_submit(ledger, *args, **kwargs)
            raise OSError("synthetic uncertain commit")

        with (
            mock.patch.object(Ledger, "submit", uncertain_submit),
            self.assertRaisesRegex(OSError, "synthetic uncertain commit"),
        ):
            authority.submit("sample", payload)
        with contextlib.closing(Ledger(self.directory)) as ledger:
            row = ledger.db.execute(
                "SELECT id FROM requests WHERE project=? AND external=?",
                ("sample", payload["id"]),
            ).fetchone()
            workspace = Path(ledger.request(row[0])["plan"]["workspace"])
        self.assertTrue(workspace.is_dir())

    def test_generic_result_is_shared_without_subscriber_destination(self):
        authority = Authority(self.directory)
        payload = self.payload()
        request = authority.submit("sample", payload)["request"]
        authority.submit(
            "sample",
            {**payload, "subscriber": {"id": "second", "wake": False}},
        )
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.schedule()[0]
            ledger.finish(stage["id"], stage["epoch"], "passed", ["db"], {})
            authority.deliver(ledger)
            self.assertEqual(
                ledger.db.execute(
                    "SELECT count(*) FROM outbox WHERE delivered=1"
                ).fetchone()[0],
                2,
            )
        result = core.load_object(
            self.project / ".orchestrator" / "resources" / request / "result.json"
        )
        self.assertNotIn("subscriber", result)

    def test_unknown_supervisor_quarantines_without_relaunch(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            ledger.schedule()
        authority.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(ledger.stages(request)[0]["state"], "recovery_required")
        self.assertEqual(authority.children, {})

    def test_runner_input_drift_releases_without_running_command(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        with contextlib.closing(Ledger(self.directory)) as ledger:
            stage = ledger.schedule()[0]
            workspace = Path(ledger.request(request)["plan"]["workspace"])
            (workspace / "input.txt").write_text("tampered")
            ledger.attach(stage["id"], stage["epoch"], {"pid": os.getpid()})
        resource_runner.execute(self.directory, stage["id"], stage["launch_token"])
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(ledger.stage(stage["id"])["state"], "invalidated")
            self.assertEqual(ledger.stage(stage["id"])["allocation"], {})

    def test_authentication_and_project_isolation(self):
        authority = Authority(self.directory)
        with self.assertRaises(ResourceError):
            authority.authenticate("sample", "bad")
        request = authority.submit("sample", self.payload())["request"]
        with (
            contextlib.closing(Ledger(self.directory)) as ledger,
            self.assertRaises(ResourceError),
        ):
            ledger.snapshot("foreign", request)

    def test_delivery_failure_does_not_retain_resource(self):
        authority = Authority(self.directory)
        request = authority.submit("sample", self.payload())["request"]
        authority.tick()
        process = next(iter(authority.children.values()))
        process.wait(timeout=15)
        with mock.patch(
            "orchestrator_engine.core.write_followup_event",
            side_effect=OSError("offline"),
        ):
            authority.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(ledger.stages(request)[0]["allocation"], {})
            self.assertEqual(
                ledger.db.execute("SELECT delivered FROM outbox").fetchone()[0], 0
            )
            ledger.db.execute("UPDATE outbox SET next_attempt=0")
        authority.tick()
        with contextlib.closing(Ledger(self.directory)) as ledger:
            self.assertEqual(
                ledger.db.execute("SELECT delivered FROM outbox").fetchone()[0], 1
            )


if __name__ == "__main__":
    unittest.main()
