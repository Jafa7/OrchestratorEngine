from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from orchestrator_engine import codex_app, core, github_actions, watcher, workers


def artifact(path: Path, value: str = "{}") -> Path:
    path.write_text(value, encoding="utf-8")
    return path


class AcceptedThenLostServer:
    starts = 0

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def notify(self, *_args, **_kwargs) -> None:
        pass

    def close(self) -> None:
        pass

    def request(self, method, params, **_kwargs):
        if method == "initialize":
            return {}
        if method == "thread/read":
            return {"thread": {"status": {"type": "idle"}}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            type(self).starts += 1
            raise ConnectionError("response lost after acceptance")
        raise AssertionError(method)


class ReliabilityRegressionTests(unittest.TestCase):
    def test_watcher_does_not_parse_already_seen_signal_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = artifact(root / "result.json")
            evidence = artifact(root / "evidence.json")
            core.write_followup_event(
                root,
                operation_id="OP-1",
                source_kind="test",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="seen-event",
            )
            state = root / "watcher-state.json"
            watcher.scan_once([root], state_path=state, action="record")
            core.signal_path_for(root, "seen-event").write_text(
                "{broken retained history", encoding="utf-8"
            )

            second = watcher.scan_once([root], state_path=state, action="record")

        self.assertEqual(second["new_count"], 0)
        self.assertEqual(second["action_errors"], [])
        self.assertEqual(second["inbox_scans"][0]["paths_skipped"], 1)
        self.assertNotIn("records_loaded", second["inbox_scans"][0])

    def test_partial_worker_result_is_quarantined_before_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_dir = root / ".orchestrator" / "tasks" / "TASK-1"
            task_dir.mkdir(parents=True)
            (task_dir / "result.json").write_bytes(b'{"terminal_status":')
            finished = core.utc_now()
            result = workers.finalize_terminal_task(
                root,
                task_id="TASK-1",
                task_dir=task_dir,
                result={"terminal_status": "completed", "finished_at": finished},
                evidence={"finished_at": finished},
                takeover=True,
            )

            evidence = core.load_object(task_dir / "evidence.json")
            quarantined = Path(evidence["recovery"]["quarantined_result_path"])
            self.assertEqual(result["outcome"], "recovered_partial")
            self.assertEqual(quarantined.read_bytes(), b'{"terminal_status":')
            self.assertEqual(
                core.load_object(task_dir / "result.json")["terminal_status"],
                "completed",
            )

    def test_conflicting_event_republication_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = artifact(root / "first.json", '{"result":1}')
            second = artifact(root / "second.json", '{"result":2}')
            evidence = artifact(root / "evidence.json")
            core.write_followup_event(
                root,
                operation_id="OP-1",
                source_kind="test",
                terminal_status="completed",
                result_path=first,
                evidence_path=evidence,
                event_id="event-1",
            )
            with self.assertRaisesRegex(core.OrchestratorError, "conflicts"):
                core.write_followup_event(
                    root,
                    operation_id="OP-1",
                    source_kind="test",
                    terminal_status="completed",
                    result_path=second,
                    evidence_path=evidence,
                    event_id="event-1",
                )

    def test_concurrent_event_publication_retains_only_one_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = artifact(root / "evidence.json")
            results = [
                artifact(root / f"result-{index}.json", f'{{"result":{index}}}')
                for index in range(2)
            ]
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def publish(result: Path) -> None:
                barrier.wait()
                try:
                    core.write_followup_event(
                        root,
                        operation_id="OP-1",
                        source_kind="test",
                        terminal_status="completed",
                        result_path=result,
                        evidence_path=evidence,
                        event_id="event-1",
                    )
                except core.OrchestratorError:
                    outcomes.append("conflict")
                else:
                    outcomes.append("published")

            threads = [
                threading.Thread(target=publish, args=(path,)) for path in results
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)

            retained = core.load_object(core.event_path_for(root, "event-1"))

        self.assertEqual(sorted(outcomes), ["conflict", "published"])
        self.assertIn(retained["result_path"], {str(path) for path in results})

    def test_headless_delivery_does_not_repeat_after_ambiguous_acceptance(self) -> None:
        AcceptedThenLostServer.starts = 0
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = artifact(root / "result.json")
            evidence = artifact(root / "evidence.json")
            core.write_followup_event(
                root,
                operation_id="OP-1",
                source_kind="test",
                terminal_status="completed",
                result_path=result,
                evidence_path=evidence,
                event_id="event-1",
            )
            signal = core.inbox(root)[0]
            calls = [
                codex_app.wake_current_thread(
                    root,
                    signal,
                    target_thread_id="thread-1",
                    server_factory=AcceptedThenLostServer,
                    recent_activity_checker=lambda *_args, **_kwargs: None,
                )
                for _ in range(2)
            ]

        self.assertEqual(AcceptedThenLostServer.starts, 1)
        self.assertTrue(
            all("headless_delivery_ambiguous" in item["reason"] for item in calls)
        )

    def test_retained_headless_claim_keeps_headless_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            core.atomic_json(
                receipt,
                {
                    "status": "delivery_claimed",
                    "delivery_mode": "headless_app_server_turn",
                    "target_thread_id": "thread-1",
                },
            )

            result = codex_app._existing_wakeup(receipt, event_id="event-1")

        self.assertEqual(result["delivery_mode"], "headless_app_server_turn")
        self.assertIn("headless_delivery_ambiguous", result["reason"])

    def test_acknowledgement_during_scan_cannot_be_overwritten(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "watcher-state.json"
            for event_id in ("event-a", "event-b"):
                result = artifact(root / f"{event_id}-result.json")
                evidence = artifact(root / f"{event_id}-evidence.json")
                core.write_followup_event(
                    root,
                    operation_id=event_id,
                    source_kind="test",
                    terminal_status="completed",
                    result_path=result,
                    evidence_path=evidence,
                    event_id=event_id,
                    wake_target={
                        "schema_version": 1,
                        "kind": "ORCHESTRATOR_WAKE_TARGET",
                        "host": "codex",
                        "target_thread_id": "thread-1",
                        "captured_at": core.utc_now(),
                    },
                )

            def adapter(_project, signal, **_kwargs):
                if signal["event_id"] == "event-a":
                    entered.set()
                    self.assertTrue(release.wait(2))
                return {"status": "woken"}

            output: list[dict] = []
            scan = threading.Thread(
                target=lambda: output.append(
                    watcher.scan_once(
                        [root],
                        state_path=state,
                        action="callback",
                        host_adapters={"codex": adapter},
                    )
                )
            )
            scan.start()
            self.assertTrue(entered.wait(2))
            acknowledgement = watcher.acknowledge_signal(
                root,
                event_id="event-b",
                host="codex",
                state_path=state,
                reason="operator owns this result",
            )
            release.set()
            scan.join(3)

            current = watcher.load_state(state)
        self.assertEqual(acknowledgement["status"], "acknowledged")
        self.assertIn("event-b", current["acknowledged_events"])
        self.assertTrue(
            any(
                item["reason"] == "operator_acknowledged"
                for item in output[0]["suppressed_signals"]
            )
        )

    def test_github_recovery_keeps_published_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor_id = "gha-recovery"
            directory = github_actions.monitor_dir_for(root, monitor_id)
            directory.mkdir(parents=True)
            check_dir = root / ".orchestrator" / "checks" / monitor_id
            check_dir.mkdir(parents=True)
            core.atomic_json(
                check_dir / "verification-result.json",
                {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
                    "check_id": monitor_id,
                },
            )
            artifact(check_dir / "summary.txt", "passed\n")
            core.atomic_json(
                directory / "evidence.json",
                {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": github_actions.EVIDENCE_KIND,
                    "monitor_id": monitor_id,
                    "monitor_status": "completed",
                    "ci_conclusion": "success",
                    "started_at": core.utc_now(),
                    "finished_at": core.utc_now(),
                },
            )
            descriptor = {
                "monitor_id": monitor_id,
                "monitor_dir": str(directory),
                "wake_policy": "always",
            }
            recovered = github_actions.recover_completed_monitor(
                root, descriptor, state_dir=".orchestrator"
            )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["ci_conclusion"], "success")


if __name__ == "__main__":
    unittest.main()
