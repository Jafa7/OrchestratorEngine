"""Provider-free clean-fixture conformance checks."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import adoption, binding, core, platform_runtime, watcher, workers

CONFORMANCE_KIND = "ORCHESTRATOR_CONFORMANCE_REPORT"
CONFORMANCE_MODES = frozenset({"auto", "portable", "full"})
SYNTHETIC_TASK_ID = "CONFORMANCE-WORKER-001"
RECOVERY_SCENARIOS = (
    "event_without_signal",
    "notification_without_seen_state",
    "result_without_evidence_or_event",
    "evidence_without_event",
    "empty_result_claim",
)
CONCURRENCY_TASK_COUNT = 6


class ConformanceError(core.OrchestratorError):
    """A clean-fixture conformance check could not be completed."""


def _bounded_error(error: BaseException) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "message": str(error)[:500],
    }


def _run_step(
    steps: list[dict[str, Any]],
    name: str,
    operation: Callable[[], Any],
    *,
    summarize: Callable[[Any], dict[str, Any]] | None = None,
) -> Any:
    started = time.monotonic()
    try:
        value = operation()
    except Exception as error:
        steps.append(
            {
                "name": name,
                "status": "failed",
                "duration_seconds": round(time.monotonic() - started, 6),
                "error": _bounded_error(error),
            }
        )
        raise
    step: dict[str, Any] = {
        "name": name,
        "status": "passed",
        "duration_seconds": round(time.monotonic() - started, 6),
    }
    if summarize is not None:
        step["details"] = summarize(value)
    steps.append(step)
    return value


def _create_fixture(fixture_root: Path | None) -> Path:
    if fixture_root is None:
        return Path(tempfile.mkdtemp(prefix="orchestrator-conformance-")).resolve()
    root = fixture_root.expanduser().resolve()
    if root.exists():
        raise ConformanceError(f"fixture root already exists: {root}")
    root.mkdir(parents=True)
    return root


def _write_synthetic_profile(project: Path, timeout_seconds: float) -> Path:
    config = workers.workers_config_path(project)
    command = (
        "import sys; "
        "payload = sys.stdin.read(); "
        "sys.exit(3) if 'ORCHESTRATOR_CONFORMANCE_INPUT v1' not in payload "
        "else None; "
        "print('CONFORMANCE_WORKER_OK')"
    )
    worker_timeout = min(timeout_seconds / 2, 10)
    config.write_text(
        "\n".join(
            [
                "[policies.quality-efficient]",
                'files = ["policies/quality-efficient.md"]',
                'quality_priority = "correctness-first"',
                "",
                "[workers.synthetic]",
                "enabled = true",
                f"command = [{json.dumps(sys.executable)}, \"-c\", "
                f"{json.dumps(command)}]",
                'prompt_via = "stdin"',
                'policy = "quality-efficient"',
                'permission_profile = "restricted"',
                f"timeout_seconds = {worker_timeout}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    prompt = project / "conformance-prompt.md"
    prompt.write_text(
        "ORCHESTRATOR_CONFORMANCE_INPUT v1\nRun the synthetic check.\n",
        encoding="utf-8",
    )
    return prompt


def _write_portable_artifacts(project: Path) -> tuple[Path, Path, str]:
    result_path, evidence_path = _write_synthetic_records(
        project,
        task_id=SYNTHETIC_TASK_ID,
        worker="synthetic-portable",
        marker="CONFORMANCE_PORTABLE_OK\n",
    )
    event_id = core.terminal_event_id(project, task_id=SYNTHETIC_TASK_ID)
    core.write_terminal_event(
        project,
        task_id=SYNTHETIC_TASK_ID,
        terminal_status="completed",
        result_path=result_path,
        evidence_path=evidence_path,
        event_id=event_id,
    )
    return result_path, evidence_path, event_id


def _write_synthetic_records(
    project: Path,
    *,
    task_id: str,
    worker: str,
    marker: str,
) -> tuple[Path, Path]:
    task_dir = workers.task_dir_for(project, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = task_dir / "stdout.log"
    stderr_path = task_dir / "stderr.log"
    stdout_path.write_text(marker, encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    now = core.utc_now()
    result_path = task_dir / "result.json"
    evidence_path = task_dir / "evidence.json"
    core.atomic_json(
        result_path,
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "WORKER_RESULT",
            "task_id": task_id,
            "worker": worker,
            "terminal_status": "completed",
            "exit_code": 0,
            "failure_reason": None,
            "duration_seconds": 0,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "started_at": now,
            "finished_at": now,
        },
    )
    core.atomic_json(
        evidence_path,
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": "WORKER_EVIDENCE",
            "task_id": task_id,
            "worker": worker,
            "command": [],
            "prompt_file": "synthetic://conformance",
            "prompt_sha256": None,
            "worker_config": {"provider_free": True},
            "started_at": now,
            "finished_at": now,
        },
    )
    return result_path, evidence_path


def _run_full_worker(project: Path, timeout_seconds: float) -> tuple[Path, Path, str]:
    prompt = _write_synthetic_profile(project, timeout_seconds)
    dispatch = workers.run_worker(
        project,
        worker="synthetic",
        task_id=SYNTHETIC_TASK_ID,
        prompt_file=prompt,
    )
    supervisor_pid = int(dispatch["supervisor_pid"])
    try:
        status = workers.wait_for_worker_task(
            project,
            task_id=SYNTHETIC_TASK_ID,
            interval_seconds=0.05,
            timeout_seconds=timeout_seconds,
            stale_after_seconds=max(timeout_seconds + 5, 30),
        )
    except Exception:
        _cancel_and_reap(project, supervisor_pid)
        raise
    if not status.get("terminal"):
        _cancel_and_reap(project, supervisor_pid)
        raise ConformanceError(
            f"synthetic worker did not finish: {status.get('wait_status', 'unknown')}"
        )
    _reap_supervisor(supervisor_pid, timeout_seconds=5)
    if status.get("status") != "completed":
        raise ConformanceError(
            f"synthetic worker finished with status {status.get('status')}"
        )
    task_dir = workers.task_dir_for(project, SYNTHETIC_TASK_ID)
    return (
        task_dir / "result.json",
        task_dir / "evidence.json",
        core.terminal_event_id(project, task_id=SYNTHETIC_TASK_ID),
    )


def _cancel_and_reap(
    project: Path,
    supervisor_pid: int,
    *,
    task_id: str = SYNTHETIC_TASK_ID,
) -> None:
    cleanup_errors: list[Exception] = []
    try:
        workers.cancel_worker_task(
            project,
            task_id=task_id,
            mode="forced",
            reason="conformance wait did not reach a terminal state",
        )
    except Exception as error:
        cleanup_errors.append(error)
    try:
        workers.wait_for_worker_task(
            project,
            task_id=task_id,
            interval_seconds=0.05,
            timeout_seconds=workers.CONTROL_POLL_SECONDS
            + workers.WORKER_TERMINATION_TIMEOUT_SECONDS
            + 5,
            stale_after_seconds=30,
        )
    except Exception as error:
        cleanup_errors.append(error)
    try:
        _reap_supervisor(supervisor_pid, timeout_seconds=5)
    except Exception as error:
        cleanup_errors.append(error)
    if cleanup_errors:
        raise ConformanceError(
            "synthetic worker cleanup failed: "
            + "; ".join(str(error)[:160] for error in cleanup_errors)
        ) from cleanup_errors[0]


def _reap_supervisor(pid: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        if time.monotonic() >= deadline:
            raise ConformanceError(
                "synthetic supervisor did not exit after finalization"
            )
        time.sleep(0.01)


def _verify_artifacts(
    project: Path,
    result_path: Path,
    evidence_path: Path,
    event_id: str,
    task_id: str = SYNTHETIC_TASK_ID,
    expected_markers: frozenset[str] = frozenset(
        {"CONFORMANCE_PORTABLE_OK\n", "CONFORMANCE_WORKER_OK\n"}
    ),
) -> dict[str, Any]:
    result = core.load_object(result_path)
    evidence = core.load_object(evidence_path)
    event_path = core.event_path_for(project, event_id)
    signal_path = core.signal_path_for(project, event_id)
    event = core.verify_terminal_event(event_path)
    signal = core.load_object(signal_path)
    if result.get("schema_version") != core.SCHEMA_VERSION:
        raise ConformanceError("result schema version is invalid")
    if result.get("kind") != "WORKER_RESULT":
        raise ConformanceError("result kind is invalid")
    if result.get("terminal_status") != "completed":
        raise ConformanceError("result is not completed")
    if evidence.get("schema_version") != core.SCHEMA_VERSION:
        raise ConformanceError("evidence schema version is invalid")
    if evidence.get("kind") != "WORKER_EVIDENCE":
        raise ConformanceError("evidence kind is invalid")
    if signal.get("schema_version") != core.SCHEMA_VERSION:
        raise ConformanceError("signal schema version is invalid")
    if signal.get("kind") != "LOCAL_AI_WORKER_FINISHED":
        raise ConformanceError("signal kind is invalid")
    for name, artifact in {
        "result": result,
        "evidence": evidence,
        "event": event,
        "signal": signal,
    }.items():
        if artifact.get("task_id") != task_id:
            raise ConformanceError(f"{name} task id is invalid")
    if result.get("worker") != evidence.get("worker"):
        raise ConformanceError("result and evidence worker identities differ")
    if event.get("event_id") != event_id or signal.get("event_id") != event_id:
        raise ConformanceError("event identity is inconsistent")
    expected_project_id = core.project_id(project)
    if event.get("project_id") != expected_project_id:
        raise ConformanceError("terminal event project identity is invalid")
    if signal.get("project_id") != expected_project_id:
        raise ConformanceError("inbox signal project identity is invalid")
    if any(
        artifact.get("terminal_status") != "completed"
        for artifact in (result, event, signal)
    ):
        raise ConformanceError("terminal status is inconsistent")
    expected_paths = {
        "result_path": result_path,
        "evidence_path": evidence_path,
        "event_path": event_path,
    }
    for field, expected in expected_paths.items():
        actual = event.get(field) if field != "event_path" else signal.get(field)
        if field in {"result_path", "evidence_path"}:
            if event.get(field) != str(expected) or signal.get(field) != str(expected):
                raise ConformanceError(f"{field} is inconsistent")
        elif actual != str(expected):
            raise ConformanceError(f"{field} is inconsistent")
    stdout_path = Path(str(result.get("stdout_path", "")))
    if not stdout_path.is_file() or stdout_path.stat().st_size > 1024:
        raise ConformanceError(
            "synthetic worker stdout is missing or unexpectedly large"
        )
    if stdout_path.read_text(encoding="utf-8") not in expected_markers:
        raise ConformanceError("synthetic worker completion marker is missing")
    return {
        "event_id": event_id,
        "result_bytes": result_path.stat().st_size,
        "evidence_bytes": evidence_path.stat().st_size,
    }


def _deliver_notification(
    project: Path,
    event_id: str,
    *,
    task_id: str = SYNTHETIC_TASK_ID,
) -> dict[str, Any]:
    first = watcher.scan_once([project], action="notify")
    second = watcher.scan_once([project], action="notify")
    if first.get("new_count") != 1 or first.get("action_errors"):
        raise ConformanceError("watcher did not deliver exactly one notification")
    if second.get("new_count") != 0:
        raise ConformanceError("watcher delivery was not idempotent")
    notifications = first.get("notifications")
    if not isinstance(notifications, list) or len(notifications) != 1:
        raise ConformanceError("watcher notification receipt is missing")
    notification = core.load_object(Path(notifications[0]))
    if notification.get("schema_version") != core.SCHEMA_VERSION:
        raise ConformanceError("watcher notification schema version is invalid")
    if notification.get("kind") != "LOCAL_AI_ORCHESTRATOR_NOTIFICATION":
        raise ConformanceError("watcher notification kind is invalid")
    if notification.get("event_id") != event_id:
        raise ConformanceError("watcher notification event id is invalid")
    if notification.get("task_id") != task_id:
        raise ConformanceError("watcher notification task id is invalid")
    if notification.get("terminal_status") != "completed":
        raise ConformanceError("watcher notification terminal status is invalid")
    state = watcher.load_state(watcher.default_state_path(project))
    if event_id not in state["seen_event_ids"]:
        raise ConformanceError("watcher state did not retain the seen event id")
    return {
        "first_scan_count": first["new_count"],
        "second_scan_count": second["new_count"],
        "seen_event_count": len(state["seen_event_ids"]),
    }


def _run_recovery_matrix(project: Path) -> dict[str, Any]:
    scenarios: list[dict[str, str]] = []
    marker = "CONFORMANCE_RECOVERY_OK\n"
    expected_markers = frozenset({marker})

    def record(name: str, task_id: str) -> None:
        event_id = core.terminal_event_id(project, task_id=task_id)
        task_dir = workers.task_dir_for(project, task_id)
        _verify_artifacts(
            project,
            task_dir / "result.json",
            task_dir / "evidence.json",
            event_id,
            task_id=task_id,
            expected_markers=expected_markers,
        )
        _deliver_notification(project, event_id, task_id=task_id)
        if len(list(core.events_root(project).glob(f"{event_id}*.json"))) != 1:
            raise ConformanceError(f"{name} produced duplicate terminal events")
        if len(
            list((core.inbox_root(project) / "signals").glob(f"{event_id}*.json"))
        ) != 1:
            raise ConformanceError(f"{name} produced duplicate inbox signals")
        if len(
            list(
                (core.inbox_root(project) / "notifications").glob(
                    f"{event_id}*.json"
                )
            )
        ) != 1:
            raise ConformanceError(f"{name} produced duplicate notifications")
        scenarios.append({"name": name, "status": "recovered", "event_id": event_id})

    task_id = "CONFORMANCE-RECOVERY-EVENT-SIGNAL"
    result_path, evidence_path = _write_synthetic_records(
        project,
        task_id=task_id,
        worker="synthetic-recovery",
        marker=marker,
    )
    event_id = core.terminal_event_id(project, task_id=task_id)
    core.write_terminal_event(
        project,
        task_id=task_id,
        terminal_status="completed",
        result_path=result_path,
        evidence_path=evidence_path,
        event_id=event_id,
    )
    core.signal_path_for(project, event_id).unlink()
    core.write_terminal_event(
        project,
        task_id=task_id,
        terminal_status="completed",
        result_path=result_path,
        evidence_path=evidence_path,
        event_id=event_id,
    )
    record("event_without_signal", task_id)

    task_id = "CONFORMANCE-RECOVERY-NOTIFICATION-STATE"
    result_path, evidence_path = _write_synthetic_records(
        project,
        task_id=task_id,
        worker="synthetic-recovery",
        marker=marker,
    )
    event_id = core.terminal_event_id(project, task_id=task_id)
    core.write_terminal_event(
        project,
        task_id=task_id,
        terminal_status="completed",
        result_path=result_path,
        evidence_path=evidence_path,
        event_id=event_id,
    )
    signal = core.load_object(core.signal_path_for(project, event_id))
    watcher.notify_signal(project, signal)
    record("notification_without_seen_state", task_id)

    task_id = "CONFORMANCE-RECOVERY-RESULT"
    result_path, evidence_path = _write_synthetic_records(
        project,
        task_id=task_id,
        worker="synthetic-recovery",
        marker=marker,
    )
    result = core.load_object(result_path)
    evidence = core.load_object(evidence_path)
    result_path.unlink()
    evidence_path.unlink()
    if not core.claim_json(result_path, result):
        raise ConformanceError("result recovery precondition could not be created")
    finalized = workers.finalize_terminal_task(
        project,
        task_id=task_id,
        task_dir=workers.task_dir_for(project, task_id),
        result=result,
        evidence=evidence,
        takeover=True,
    )
    if finalized.get("outcome") != "reconciled":
        raise ConformanceError("existing result was not reconciled")
    record("result_without_evidence_or_event", task_id)

    task_id = "CONFORMANCE-RECOVERY-EVIDENCE"
    result_path, evidence_path = _write_synthetic_records(
        project,
        task_id=task_id,
        worker="synthetic-recovery",
        marker=marker,
    )
    result = core.load_object(result_path)
    evidence = core.load_object(evidence_path)
    finalized = workers.finalize_terminal_task(
        project,
        task_id=task_id,
        task_dir=workers.task_dir_for(project, task_id),
        result=result,
        evidence=evidence,
        takeover=True,
    )
    if finalized.get("outcome") != "reconciled":
        raise ConformanceError("existing result and evidence were not reconciled")
    record("evidence_without_event", task_id)

    task_id = "CONFORMANCE-RECOVERY-EMPTY-CLAIM"
    result_path, evidence_path = _write_synthetic_records(
        project,
        task_id=task_id,
        worker="synthetic-recovery",
        marker=marker,
    )
    result = core.load_object(result_path)
    evidence = core.load_object(evidence_path)
    result_path.write_text("", encoding="utf-8")
    evidence_path.unlink()
    finalized = workers.finalize_terminal_task(
        project,
        task_id=task_id,
        task_dir=workers.task_dir_for(project, task_id),
        result=result,
        evidence=evidence,
        takeover=True,
    )
    if finalized.get("outcome") != "claimed":
        raise ConformanceError("empty result claim was not recovered")
    record("empty_result_claim", task_id)

    if tuple(item["name"] for item in scenarios) != RECOVERY_SCENARIOS:
        raise ConformanceError("recovery scenarios did not complete deterministically")
    return {
        "scenario_count": len(RECOVERY_SCENARIOS),
        "recovered_count": len(scenarios),
        "scenarios": scenarios,
    }


def _run_lifecycle_recovery(project: Path) -> dict[str, Any]:
    task_id = "CONFORMANCE-RECOVERY-UNCLAIMED-TASK"
    task_dir = workers.task_dir_for(project, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    prompt = project / "lifecycle-recovery-prompt.md"
    prompt.write_text("Synthetic lifecycle recovery.\n", encoding="utf-8")
    created_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    bound = binding.write_binding(
        project,
        host="codex",
        target_thread_id="conformance-codex-thread",
    )
    core.atomic_json(
        task_dir / "task.json",
        {
            "schema_version": core.SCHEMA_VERSION,
            "kind": workers.TASK_KIND,
            "task_id": task_id,
            "worker": "concurrent",
            "status": "starting",
            "prompt_file": str(prompt),
            "prompt_sha256": core.sha256_file(prompt),
            "task_dir": str(task_dir),
            "created_at": created_at,
            "lease_required": True,
            "wake_target": binding.wake_target_from_binding(bound),
        },
    )
    first = workers.reap_worker_tasks(project, now=datetime.now(UTC))
    second = workers.reap_worker_tasks(project, now=datetime.now(UTC))
    if first.get("reaped_count") != 1 or second.get("reaped_count") != 0:
        raise ConformanceError("unclaimed task did not converge after reaping")
    result = core.load_object(task_dir / "result.json")
    evidence = core.load_object(task_dir / "evidence.json")
    descriptor = core.load_object(task_dir / "task.json")
    event_id = core.terminal_event_id(project, task_id=task_id)
    event = core.verify_terminal_event(core.event_path_for(project, event_id))
    signal = core.load_object(core.signal_path_for(project, event_id))
    if result.get("failure_class") != "supervisor_lost":
        raise ConformanceError("reaped task result has the wrong failure class")
    if evidence.get("recovery", {}).get("reason") != "supervisor_lost":
        raise ConformanceError("reaped task evidence has the wrong recovery reason")
    if any(
        artifact.get("terminal_status") != "failed"
        for artifact in (result, event, signal)
    ) or descriptor.get("status") != "failed":
        raise ConformanceError("reaped task terminal status is inconsistent")
    state_path = watcher.default_host_state_path(project, host="codex")
    delivered = watcher.scan_once(
        [project],
        action="record",
        state_path=state_path,
        host_filter={"codex"},
    )
    repeated = watcher.scan_once(
        [project],
        action="record",
        state_path=state_path,
        host_filter={"codex"},
    )
    if delivered.get("new_count") != 1 or repeated.get("new_count") != 0:
        raise ConformanceError("reaped task signal was not consumed exactly once")
    return {
        "status": "passed",
        "reaped_count": 1,
        "second_reaped_count": 0,
        "terminal_status": "failed",
        "failure_class": "supervisor_lost",
    }


def _write_concurrency_profile(project: Path, timeout_seconds: float) -> Path:
    config = workers.workers_config_path(project)
    command = (
        "import sys, time; "
        "payload = sys.stdin.read(); "
        "time.sleep(0.1); "
        "sys.exit(3) if 'ORCHESTRATOR_CONCURRENCY_INPUT v1' not in payload "
        "else None; "
        "print('CONFORMANCE_CONCURRENCY_OK')"
    )
    worker_timeout = min(timeout_seconds / 2, 10)
    config.write_text(
        "\n".join(
            [
                "[policies.quality-efficient]",
                'files = ["policies/quality-efficient.md"]',
                'quality_priority = "correctness-first"',
                "",
                "[workers.concurrent]",
                "enabled = true",
                f"command = [{json.dumps(sys.executable)}, \"-c\", "
                f"{json.dumps(command)}]",
                'prompt_via = "stdin"',
                'policy = "quality-efficient"',
                'permission_profile = "restricted"',
                f"timeout_seconds = {worker_timeout}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    prompt = project / "concurrency-prompt.md"
    prompt.write_text(
        "ORCHESTRATOR_CONCURRENCY_INPUT v1\nRun the synthetic check.\n",
        encoding="utf-8",
    )
    return prompt


def _run_concurrency_check(project: Path, timeout_seconds: float) -> dict[str, Any]:
    prompt = _write_concurrency_profile(project, timeout_seconds)
    dispatched: list[tuple[str, int, str]] = []
    expected_host_counts = {"codex": 0, "vscode": 0}
    try:
        for index in range(CONCURRENCY_TASK_COUNT):
            task_id = f"CONFORMANCE-CONCURRENT-{index + 1:02d}"
            host = "codex" if index % 2 == 0 else "vscode"
            binding.write_binding(
                project,
                host=host,
                target_thread_id=(
                    "conformance-codex-thread" if host == "codex" else None
                ),
            )
            dispatch = workers.run_worker(
                project,
                worker="concurrent",
                task_id=task_id,
                prompt_file=prompt,
                allow_duplicate=True,
                duplicate_reason="provider-free conformance concurrency check",
            )
            dispatched.append((task_id, int(dispatch["supervisor_pid"]), host))
            expected_host_counts[host] += 1

        task_ids = [task_id for task_id, _pid, _host in dispatched]
        any_status = workers.wait_for_worker_tasks(
            project,
            task_ids=task_ids,
            mode="any",
            interval_seconds=0.02,
            timeout_seconds=timeout_seconds,
            stale_after_seconds=max(timeout_seconds + 5, 30),
        )
        if not any_status.get("condition_met"):
            raise ConformanceError("concurrent wait-any did not reach a terminal task")
        all_status = workers.wait_for_worker_tasks(
            project,
            task_ids=task_ids,
            mode="all",
            interval_seconds=0.02,
            timeout_seconds=timeout_seconds,
            stale_after_seconds=max(timeout_seconds + 5, 30),
        )
        if (
            not all_status.get("condition_met")
            or all_status.get("completed_count") != CONCURRENCY_TASK_COUNT
        ):
            raise ConformanceError("concurrent wait-all did not complete every task")
        for _task_id, supervisor_pid, _host in dispatched:
            _reap_supervisor(supervisor_pid, timeout_seconds=5)
    except Exception:
        for task_id, supervisor_pid, _host in dispatched:
            try:
                status = workers.worker_wait_snapshot(project, task_id=task_id)
                if status.get("terminal"):
                    _reap_supervisor(supervisor_pid, timeout_seconds=5)
                else:
                    _cancel_and_reap(
                        project,
                        supervisor_pid,
                        task_id=task_id,
                    )
            except Exception:
                pass
        raise

    expected_markers = frozenset({"CONFORMANCE_CONCURRENCY_OK\n"})
    for task_id, _supervisor_pid, expected_host in dispatched:
        task_dir = workers.task_dir_for(project, task_id)
        event_id = core.terminal_event_id(project, task_id=task_id)
        _verify_artifacts(
            project,
            task_dir / "result.json",
            task_dir / "evidence.json",
            event_id,
            task_id=task_id,
            expected_markers=expected_markers,
        )
        event = core.load_object(core.event_path_for(project, event_id))
        wake_target = event.get("wake_target")
        if (
            not isinstance(wake_target, dict)
            or wake_target.get("host") != expected_host
        ):
            raise ConformanceError(f"{task_id} wake target was not snapshotted")

    delivered_host_counts: dict[str, int] = {}
    for host in ("codex", "vscode"):
        state_path = watcher.default_host_state_path(project, host=host)
        first = watcher.scan_once(
            [project],
            action="record",
            state_path=state_path,
            host_filter={host},
        )
        second = watcher.scan_once(
            [project],
            action="record",
            state_path=state_path,
            host_filter={host},
        )
        if first.get("new_count") != expected_host_counts[host]:
            raise ConformanceError(f"{host} watcher consumed the wrong signal count")
        if second.get("new_count") != 0:
            raise ConformanceError(f"{host} watcher state was not idempotent")
        delivered_host_counts[host] = int(first["new_count"])

    return {
        "status": "passed",
        "task_count": CONCURRENCY_TASK_COUNT,
        "wait_any_terminal_count": int(any_status["terminal_count"]),
        "wait_all_terminal_count": int(all_status["terminal_count"]),
        "expected_host_counts": expected_host_counts,
        "delivered_host_counts": delivered_host_counts,
    }


def _artifact_summary(project: Path) -> dict[str, int]:
    state = core.state_root(project)
    return {
        "task_descriptor_count": len(
            list(workers.tasks_root(project).glob("*/task.json"))
        ),
        "result_count": len(list(workers.tasks_root(project).glob("*/result.json"))),
        "evidence_count": len(
            list(workers.tasks_root(project).glob("*/evidence.json"))
        ),
        "event_count": len(list(core.events_root(project).glob("*.json"))),
        "signal_count": len(
            list((core.inbox_root(project) / "signals").glob("*.json"))
        ),
        "notification_count": len(
            list((core.inbox_root(project) / "notifications").glob("*.json"))
        ),
        "json_artifact_count": len(list(state.rglob("*.json"))),
    }


def run_conformance(
    *,
    mode: str = "auto",
    fixture_root: Path | None = None,
    keep_fixture: bool = False,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Run a provider-free conformance check in a new isolated directory."""

    if mode not in CONFORMANCE_MODES:
        raise ConformanceError(
            "conformance mode must be one of: " + ", ".join(sorted(CONFORMANCE_MODES))
        )
    if not math.isfinite(timeout_seconds) or timeout_seconds < 1:
        raise ConformanceError("timeout_seconds must be finite and at least 1")
    detached = platform_runtime.detached_lifecycle_supported()
    effective_mode = "full" if mode == "auto" and detached else mode
    if effective_mode == "auto":
        effective_mode = "portable"
    started_at = core.utc_now()
    started = time.monotonic()
    attempted_root = str(fixture_root) if fixture_root is not None else None
    steps: list[dict[str, Any]] = []
    root: Path | None = None
    failure: dict[str, str] | None = None
    summary: dict[str, int] = {}
    recovery_summary: dict[str, Any] = {
        "scenario_count": len(RECOVERY_SCENARIOS),
        "recovered_count": 0,
        "scenarios": [],
    }
    concurrency_summary: dict[str, Any] = {
        "status": "not_run" if effective_mode == "full" else "skipped",
        "task_count": 0,
        "reason": (
            "earlier_step_failed"
            if effective_mode == "full"
            else "full_mode_required"
        ),
    }
    lifecycle_recovery_summary: dict[str, Any] = {
        "status": "not_run" if effective_mode == "full" else "skipped",
        "reason": (
            "earlier_step_failed"
            if effective_mode == "full"
            else "full_mode_required"
        ),
    }
    try:
        root = _run_step(
            steps,
            "create_clean_fixture",
            lambda: _create_fixture(fixture_root),
        )
        if effective_mode == "full":
            _run_step(
                steps,
                "verify_detached_capability",
                lambda: platform_runtime.require_detached_lifecycle(
                    "full conformance"
                ),
            )
        _run_step(
            steps,
            "adopt_clean_fixture",
            lambda: adoption.adopt_project(root),
            summarize=lambda value: {
                "created_count": len(value["created"]),
                "status": value["status"],
            },
        )
        if effective_mode == "full":
            artifacts = _run_step(
                steps,
                "run_synthetic_worker",
                lambda: _run_full_worker(root, timeout_seconds),
                summarize=lambda value: {"task_id": SYNTHETIC_TASK_ID},
            )
        else:
            artifacts = _run_step(
                steps,
                "write_portable_artifacts",
                lambda: _write_portable_artifacts(root),
                summarize=lambda value: {"task_id": SYNTHETIC_TASK_ID},
            )
        result_path, evidence_path, event_id = artifacts
        _run_step(
            steps,
            "verify_durable_artifacts",
            lambda: _verify_artifacts(
                root, result_path, evidence_path, event_id
            ),
            summarize=lambda value: value,
        )
        _run_step(
            steps,
            "deliver_idempotent_notification",
            lambda: _deliver_notification(root, event_id),
            summarize=lambda value: value,
        )
        recovery_summary = _run_step(
            steps,
            "verify_crash_recovery_matrix",
            lambda: _run_recovery_matrix(root),
            summarize=lambda value: {
                "scenario_count": value["scenario_count"],
                "recovered_count": value["recovered_count"],
            },
        )
        if effective_mode == "full":
            concurrency_summary = _run_step(
                steps,
                "verify_concurrent_workers_and_host_routing",
                lambda: _run_concurrency_check(root, timeout_seconds),
                summarize=lambda value: {
                    "task_count": value["task_count"],
                    "wait_all_terminal_count": value["wait_all_terminal_count"],
                    "delivered_host_counts": value["delivered_host_counts"],
                },
            )
            lifecycle_recovery_summary = _run_step(
                steps,
                "verify_unclaimed_task_recovery",
                lambda: _run_lifecycle_recovery(root),
                summarize=lambda value: value,
            )
        summary = _artifact_summary(root)
        status = "passed"
    except Exception as error:
        status = "failed"
        failure = _bounded_error(error)

    retain = root is not None and (keep_fixture or status == "failed")
    if root is not None and not retain:
        try:
            shutil.rmtree(root)
        except OSError as error:
            status = "failed"
            failure = _bounded_error(error)
            retain = True
    finished_at = core.utc_now()
    if root is None:
        fixture_status = "not_created"
        fixture_reason = "creation_failed"
        reported_root = attempted_root
    else:
        fixture_status = "retained" if retain else "removed"
        fixture_reason = "failure" if status == "failed" else (
            "requested" if keep_fixture else "success_cleanup"
        )
        reported_root = str(root) if retain else None
    report: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": CONFORMANCE_KIND,
        "status": status,
        "requested_mode": mode,
        "effective_mode": effective_mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(time.monotonic() - started, 6),
        "capabilities": platform_runtime.capabilities(),
        "steps": steps,
        "artifact_summary": summary,
        "recovery_summary": recovery_summary,
        "concurrency_summary": concurrency_summary,
        "lifecycle_recovery_summary": lifecycle_recovery_summary,
        "fixture": {
            "status": fixture_status,
            "root": reported_root,
            "reason": fixture_reason,
        },
    }
    if failure is not None:
        report["failure"] = failure
    return report
