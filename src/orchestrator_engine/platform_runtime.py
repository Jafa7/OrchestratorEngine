"""Small platform boundary for locking and detached-runtime capability."""

from __future__ import annotations

import contextlib
import os
import platform
import sys
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
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold one blocking, process-wide advisory lock for ``path``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _lock(handle)
        except OSError as error:
            raise PlatformRuntimeError(
                f"could not acquire advisory lock: {path}"
            ) from error
        try:
            yield
        finally:
            _unlock(handle)


def detached_lifecycle_supported() -> bool:
    """Return whether identity-safe detached lifecycle is implemented."""

    return sys.platform.startswith("linux") and Path("/proc/self/stat").is_file()


def require_detached_lifecycle(feature: str) -> None:
    if detached_lifecycle_supported():
        return
    raise PlatformRuntimeError(
        f"{feature} requires the Linux detached-runtime capability; "
        "use Linux or WSL, or run a supported foreground operation"
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
