"""Supervisor-owned task lease, process identity and identity-safe signalling.

A task lease records which process drives a task and proves that process is
still the one that took the lease. A pid alone cannot prove that: pids are
recycled, so a recorded pid may later belong to an unrelated process. Every
decision that ends a task or sends a signal therefore compares a process
identity token — pid plus its kernel start time plus the current boot id — and
refuses to act when the token no longer matches.
"""

from __future__ import annotations

import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import core

LEASE_KIND = "WORKER_LEASE"
LEASE_NAME = "lease.json"
IDENTITY_SOURCE = "linux-proc-stat"
# The supervisor renews on this cadence; the lease is only considered expired
# after several missed renewals, so a briefly delayed supervisor is not reaped.
DEFAULT_LEASE_INTERVAL_SECONDS = 30.0
LEASE_EXPIRY_FACTOR = 3.0
DEFAULT_LEASE_EXPIRY_SECONDS = DEFAULT_LEASE_INTERVAL_SECONDS * LEASE_EXPIRY_FACTOR
# `/proc/<pid>/stat` field 22 is the process start time in clock ticks since
# boot. It is assigned by the kernel and never changes, so (boot, pid, start
# time) identifies one process for the life of the machine.
PROC_STAT_STARTTIME_INDEX = 19
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
ZOMBIE_STATE = "Z"

WORKER_STOP_GRACE_SECONDS = 10.0
WORKER_STOP_TIMEOUT_SECONDS = 10.0
WORKER_STOP_POLL_SECONDS = 0.1


class WorkerLeaseError(RuntimeError):
    """A deterministic worker lease failure."""


def boot_id() -> str | None:
    try:
        return BOOT_ID_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def process_identity(pid: object) -> dict[str, Any] | None:
    """Return the identity token of a live pid, or None if there is no process.

    The token includes the process state so a caller can tell an exited-but-not
    yet-reaped process (a zombie, which can no longer do any work) from a
    running one, while still keeping its pid reserved and therefore safe to
    signal.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The comm field is parenthesised and may itself contain spaces and
    # parentheses, so fields are only unambiguous after the final ") ".
    _, separator, tail = stat.rpartition(") ")
    if not separator:
        return None
    fields = tail.split()
    try:
        start_ticks = int(fields[PROC_STAT_STARTTIME_INDEX])
    except (IndexError, ValueError):
        return None
    identity: dict[str, Any] = {
        "source": IDENTITY_SOURCE,
        "pid": pid,
        "start_ticks": start_ticks,
        "state": fields[0],
    }
    current_boot = boot_id()
    if current_boot is not None:
        identity["boot_id"] = current_boot
    return identity


def identity_matches(recorded: object, observed: dict[str, Any] | None) -> bool:
    """Report whether an observed process is the exact process that was recorded.

    `state` is deliberately excluded: a process that has exited but is not yet
    reaped is still the same process, and its pid is still reserved.
    """
    if not isinstance(recorded, dict) or observed is None:
        return False
    for key in ("pid", "start_ticks"):
        if recorded.get(key) != observed.get(key):
            return False
    recorded_boot = recorded.get("boot_id")
    observed_boot = observed.get("boot_id")
    if recorded_boot is not None and observed_boot is not None:
        return recorded_boot == observed_boot
    return True


def identity_state(recorded: object) -> dict[str, Any]:
    """Classify a recorded process identity against the live process table.

    - `alive`: the recorded process is still running.
    - `gone`: the process exited (or its pid now belongs to something else, or
      the machine rebooted). This is proof, not a guess.
    - `unknown`: nothing was recorded, so nothing can be proven. Callers must
      fail closed: never signal, and only finalize on an explicit staleness
      threshold.
    """
    if not isinstance(recorded, dict) or not isinstance(recorded.get("pid"), int):
        return {"state": "unknown", "identity_verified": False, "observed": None}
    observed = process_identity(recorded["pid"])
    if not identity_matches(recorded, observed):
        # No process, or a different process reusing the pid. Either way the
        # recorded process no longer exists and must never be signalled.
        return {"state": "gone", "identity_verified": False, "observed": observed}
    assert observed is not None
    if observed.get("state") == ZOMBIE_STATE:
        return {"state": "gone", "identity_verified": True, "observed": observed}
    return {"state": "alive", "identity_verified": True, "observed": observed}


def pid_state(pid: object) -> dict[str, Any]:
    """Classify a pid with no recorded identity token (pre-lease task).

    Without a token a live pid cannot be proven to be the original process, so
    an occupied pid is reported as `alive` and the task is left alone. Only an
    unoccupied pid proves the recorded process is gone.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"state": "unknown", "identity_verified": False, "observed": None}
    observed = process_identity(pid)
    if observed is None:
        return {"state": "gone", "identity_verified": False, "observed": None}
    if observed.get("state") == ZOMBIE_STATE:
        return {"state": "gone", "identity_verified": False, "observed": observed}
    return {"state": "alive", "identity_verified": False, "observed": observed}


def lease_path(task_dir: Path) -> Path:
    return task_dir / LEASE_NAME


def load_lease(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        lease = core.load_object(path)
    except (OSError, core.OrchestratorError) as error:
        raise WorkerLeaseError(f"task lease is unreadable: {path}: {error}") from error
    if lease.get("kind") != LEASE_KIND:
        raise WorkerLeaseError(f"task lease has unexpected kind: {path}")
    return lease


def acquire_lease(
    task_dir: Path,
    *,
    task_id: str,
    worker: str,
    interval_seconds: float = DEFAULT_LEASE_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Take the supervisor lease for a task and record who holds it."""

    pid = os.getpid()
    identity = process_identity(pid)
    if identity is None:
        raise WorkerLeaseError(
            f"cannot read a process identity token for supervisor pid {pid}; "
            "the lease requires a Linux /proc filesystem"
        )
    now = core.utc_now()
    lease = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": LEASE_KIND,
        "task_id": task_id,
        "worker": worker,
        "status": "held",
        "holder": "supervisor",
        "supervisor_pid": pid,
        "supervisor_identity": identity,
        "lease_interval_seconds": float(interval_seconds),
        "lease_expiry_seconds": float(interval_seconds) * LEASE_EXPIRY_FACTOR,
        "acquired_at": now,
        "renewed_at": now,
    }
    core.atomic_json(lease_path(task_dir), lease)
    return lease


def record_worker_identity(
    lease: dict[str, Any],
    task_dir: Path,
    *,
    worker_pid: int,
    worker_pgid: int | None,
) -> dict[str, Any]:
    """Record the worker's identity in the lease before the supervisor waits.

    A supervisor can die at any moment. If it dies after spawning the worker,
    this is the only durable record that lets a reaper stop the orphaned worker
    tree without risking a signal to a recycled pid.
    """
    identity = process_identity(worker_pid)
    lease["worker_pid"] = worker_pid
    if worker_pgid is not None:
        lease["worker_pgid"] = worker_pgid
    if identity is not None:
        lease["worker_identity"] = identity
    return renew_lease(lease, task_dir)


def renew_lease(lease: dict[str, Any], task_dir: Path) -> dict[str, Any]:
    lease["renewed_at"] = core.utc_now()
    core.atomic_json(lease_path(task_dir), lease)
    return lease


def release_lease(
    lease: dict[str, Any],
    task_dir: Path,
    *,
    released_by: str,
    terminal_status: str,
) -> dict[str, Any]:
    lease["status"] = "released"
    lease["released_by"] = released_by
    lease["terminal_status"] = terminal_status
    lease["released_at"] = core.utc_now()
    core.atomic_json(lease_path(task_dir), lease)
    return lease


def lease_age_seconds(lease: dict[str, Any], *, now: datetime) -> float | None:
    stamp = lease.get("renewed_at") or lease.get("acquired_at")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max((now - parsed.astimezone(UTC)).total_seconds(), 0.0)


def stop_worker_tree(
    *,
    worker_pid: object,
    worker_pgid: object,
    worker_identity: object,
    reason: str,
    grace_seconds: float = WORKER_STOP_GRACE_SECONDS,
    timeout_seconds: float = WORKER_STOP_TIMEOUT_SECONDS,
    poll_seconds: float = WORKER_STOP_POLL_SECONDS,
) -> dict[str, Any]:
    """Stop an orphaned worker, but only while its recorded identity holds.

    The caller here is not the worker's parent, so it cannot hold the pid
    reserved by leaving a zombie unreaped. Identity is therefore re-verified
    immediately before every signal, and a mismatch means the recorded worker is
    gone and something else now owns the pid: the signal is refused, never sent
    speculatively.
    """
    ledger: dict[str, Any] = {
        "reason": reason,
        "scope": "process",
        "process_group": None,
        "grace_seconds": float(grace_seconds),
        "escalated": False,
        "exited": True,
        "identity_verified": False,
        "signals": [],
    }
    if not isinstance(worker_pid, int) or isinstance(worker_pid, bool):
        ledger["stop_outcome"] = "no_worker_recorded"
        return ledger

    state = identity_state(worker_identity)
    if state["state"] == "unknown":
        # A worker pid with no identity token cannot be signalled safely: a live
        # pid may already belong to an unrelated process.
        ledger["stop_outcome"] = "refused_no_identity_token"
        ledger["exited"] = process_identity(worker_pid) is None
        return ledger
    if state["state"] == "gone":
        ledger["stop_outcome"] = (
            "refused_identity_mismatch"
            if state["observed"] is not None
            else "already_exited"
        )
        return ledger

    ledger["identity_verified"] = True
    ledger["exited"] = False
    # Only a group the worker itself leads may be signalled: the pgid is then
    # reserved for as long as the leader pid is, so it cannot be recycled under
    # us the way an arbitrary recorded group could.
    group = worker_pgid if worker_pgid == worker_pid else None
    if isinstance(group, int) and not isinstance(group, bool):
        ledger["scope"] = "process_group"
        ledger["process_group"] = group

    def deliver(sent: signal.Signals) -> bool:
        if not identity_matches(worker_identity, process_identity(worker_pid)):
            return False
        try:
            if isinstance(group, int):
                os.killpg(group, sent)
            else:
                os.kill(worker_pid, sent)
        except OSError:
            return False
        ledger["signals"].append(
            {"signal": sent.name, "scope": ledger["scope"], "at": core.utc_now()}
        )
        return True

    deliver(signal.SIGTERM)
    if wait_until_gone(
        worker_pid,
        worker_identity,
        timeout_seconds=grace_seconds,
        poll_seconds=poll_seconds,
    ):
        ledger["exited"] = True
        # The worker stopped on SIGTERM, so its pid may now be reaped by init and
        # recycled at any moment. Sweeping the group for surviving descendants
        # would mean signalling a group id we can no longer prove.
        ledger["stop_outcome"] = "stopped_on_sigterm"
        ledger["descendant_sweep"] = "skipped_unverifiable_group"
        return ledger

    ledger["escalated"] = True
    deliver(signal.SIGKILL)
    ledger["exited"] = wait_until_gone(
        worker_pid,
        worker_identity,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )
    ledger["stop_outcome"] = "killed" if ledger["exited"] else "kill_not_confirmed"
    return ledger


def wait_until_gone(
    pid: int,
    identity: object,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> bool:
    """Wait until the recorded process is no longer running.

    Liveness is identity-based, so a pid that is recycled while we wait counts as
    gone rather than as a still-running worker.
    """
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        if identity_state(identity)["state"] != "alive":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_seconds)
