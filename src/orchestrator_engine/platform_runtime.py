"""Small platform boundary for locking and detached-runtime capability."""

from __future__ import annotations

import contextlib
import os
import platform
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from . import core

PLATFORM_CAPABILITIES_KIND = "ORCHESTRATOR_PLATFORM_CAPABILITIES"


class PlatformRuntimeError(core.OrchestratorError):
    """The current platform cannot provide a required runtime guarantee."""


def _lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _try_lock(handle: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _windows_process_alive(pid: int) -> bool:
    """Check a Windows process handle without sending a control signal."""

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        if error == error_access_denied:
            return True
        # An unfamiliar query failure does not prove process absence.
        return True
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == wait_object_0:
            return False
        if result == wait_timeout:
            return True
        # A failed or unfamiliar wait does not prove process absence.
        return True
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    """Return whether a same-platform pid exists without signalling it."""

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Hold one blocking, process-wide advisory lock for ``path``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        if timeout_seconds is None:
            try:
                _lock(handle)
            except OSError as error:
                raise PlatformRuntimeError(
                    f"could not acquire advisory lock: {path}"
                ) from error
        else:
            if timeout_seconds < 0:
                raise ValueError("timeout_seconds must be non-negative")
            deadline = time.monotonic() + timeout_seconds
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise PlatformRuntimeError(
                        f"timed out acquiring advisory lock: {path}"
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            _unlock(handle)


def detached_lifecycle_supported() -> bool:
    """Return whether identity-safe detached lifecycle is implemented."""

    if sys.platform.startswith("linux"):
        return Path("/proc/self/stat").is_file()
    try:
        if sys.platform == "darwin":
            from . import runtime_macos

            return runtime_macos.probe(os.getpid())["state"] == "present"
        if sys.platform == "win32":
            from . import runtime_windows

            return runtime_windows.probe(os.getpid())["state"] == "present"
    except (OSError, AttributeError):
        return False
    return False


def require_detached_lifecycle(feature: str) -> None:
    if detached_lifecycle_supported():
        return
    raise PlatformRuntimeError(
        f"{feature} requires an available identity-safe detached-runtime capability; "
        "inspect runtime-capabilities or run a supported foreground operation"
    )


def capabilities() -> dict[str, object]:
    detached = detached_lifecycle_supported()
    return {
        "schema_version": core.SCHEMA_VERSION,
        "kind": PLATFORM_CAPABILITIES_KIND,
        "os_name": os.name,
        "platform": sys.platform,
        "platform_system": platform.system(),
        "portable_core": "supported",
        "file_locking": "supported",
        "detached_lifecycle": "supported" if detached else "unsupported",
        "recommended_runtime": None if detached else "linux-or-wsl",
    }


def spawn(factory, args, *, owned: bool = False, **kwargs):
    """Launch a supervisor or owned command through the native process boundary.

    Injected factories retain their existing contract for deterministic tests.
    A command job is killed if its owning Windows supervisor disappears; a
    detached supervisor outlives the dispatching CLI.
    """
    if os.name == "nt" and factory is subprocess.Popen:
        from .runtime_windows import JobProcess

        return JobProcess(args, owned=owned, **kwargs)
    return factory(args, **kwargs)


def process_group(pid: int) -> int | None:
    if os.name == "nt":
        from . import runtime_windows

        identity = runtime_windows.probe(pid)["identity"]
        return pid if runtime_windows.job_state(identity) == "alive" else None
    try:
        group = os.getpgid(pid)
        return group if group == pid and group != os.getpgid(0) else None
    except OSError:
        return None


def signal_group(pgid: int, sent: int, *, identity: object = None) -> None:
    if os.name == "nt":
        from . import runtime_windows

        if identity is None:
            identity = runtime_windows.probe(pgid)["identity"]
        if not isinstance(identity, dict) or identity.get("pid") != pgid:
            raise OSError("process job identity is unavailable")
        if not runtime_windows.terminate_job(identity):
            raise OSError("process job termination was not confirmed")
        return
    os.killpg(pgid, sent)


def stop_owned(process, *, reason: str, timeout: float = 10.0) -> dict:
    """Terminate a Windows job through its durable identity, then reap the child."""
    from . import runtime_windows

    identity = getattr(process, "runtime_identity", None)
    if identity is None:
        raise OSError("owned Windows job identity is unavailable")
    exited = runtime_windows.terminate_job(identity, timeout)
    if exited:
        process.wait(timeout=timeout)
    return {
        "reason": reason,
        "scope": "job_object",
        "process_group": process.pid,
        "grace_seconds": 0.0,
        "escalated": True,
        "exited": exited,
        "signals": [
            {
                "signal": "TerminateJobObject",
                "scope": "job_object",
                "at": core.utc_now(),
            }
        ],
    }
