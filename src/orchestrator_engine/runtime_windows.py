"""Windows process identities and named Job Object containment.

An inherited pipe gates the internal launcher until job assignment succeeds.
Owned commands use kill-on-close; detached supervisors retain their job when
the dispatching CLI exits. Reapers open the recorded job by creation identity,
never by a bare PID. No breakaway flags are enabled.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import os
import subprocess
import sys
import time
from ctypes import wintypes as w
from functools import lru_cache
from pathlib import Path

SOURCE = "windows-process-times"
QUERY = 0x1000
SYNCHRONIZE = 0x100000
JOB_QUERY = 4
JOB_TERMINATE = 8


@lru_cache(maxsize=1)
def machine_id() -> str:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
    if not isinstance(value, str) or not value:
        raise OSError("Windows machine identity is unavailable")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", w.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_limit", w.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_int64 * 4),
        ("page_faults", w.DWORD),
        ("total", w.DWORD),
        ("active", w.DWORD),
        ("terminated", w.DWORD),
    ]


@lru_cache(maxsize=1)
def api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "CreateFileW": (
            [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE],
            w.HANDLE,
        ),
        "GetCurrentProcess": ([], w.HANDLE),
        "CreateMutexW": ([ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
        "ReleaseMutex": ([w.HANDLE], w.BOOL),
        "DuplicateHandle": (
            [
                w.HANDLE,
                w.HANDLE,
                w.HANDLE,
                ctypes.POINTER(w.HANDLE),
                w.DWORD,
                w.BOOL,
                w.DWORD,
            ],
            w.BOOL,
        ),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "GetProcessTimes": ([w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "OpenJobObjectW": ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": (
            [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD],
            w.BOOL,
        ),
        "QueryInformationJobObject": (
            [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)],
            w.BOOL,
        ),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = args
        function.restype = result
    return kernel


def check(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


@contextlib.contextmanager
def file_access(path: Path):
    """Serialize native atomic replacement/read without creating lock artifacts."""
    canonical = os.path.normcase(str(path.resolve()))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    handle = check(
        api().CreateMutexW(None, False, f"Local\\OrchestratorEngine.File.{digest}")
    )
    acquired = False
    try:
        result = api().WaitForSingleObject(handle, 5000)
        # An abandoned mutex transfers ownership; the atomic file remains
        # either the old or new complete version after writer failure.
        acquired = result in (0, 0x80)
        if not acquired:
            raise OSError("native state-file mutex could not be acquired")
        yield
    finally:
        if acquired:
            api().ReleaseMutex(handle)
        api().CloseHandle(handle)


def read_shared_text(path: Path) -> str:
    with file_access(path):
        return _read_shared_text(path)


def _read_shared_text(path: Path) -> str:
    """Allow atomic replacement while this reader holds the old file object."""
    import msvcrt

    handle = api().CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api().CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        return stream.read()


def identity_from_handle(handle, pid: int) -> dict:
    times = [w.FILETIME() for _ in range(4)]
    check(api().GetProcessTimes(handle, *(ctypes.byref(t) for t in times)))
    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    status = api().WaitForSingleObject(handle, 0)
    if status not in (0, 258):
        raise OSError("process wait state is unavailable")
    return {
        "source": SOURCE,
        "machine_id": machine_id(),
        "pid": pid,
        "start_ticks": ticks,
        "state": "Z" if status == 0 else "R",
    }


def probe(pid: int) -> dict:
    handle = api().OpenProcess(QUERY | SYNCHRONIZE, False, pid)
    if not handle:
        return {
            "state": "gone" if ctypes.get_last_error() == 87 else "unknown",
            "identity": None,
        }
    try:
        return {"state": "present", "identity": identity_from_handle(handle, pid)}
    except OSError:
        return {"state": "unknown", "identity": None}
    finally:
        api().CloseHandle(handle)


def job_name(identity: object) -> str:
    if not isinstance(identity, dict) or identity.get("source") != SOURCE:
        raise ValueError("a Windows process identity is required")
    if identity.get("machine_id") != machine_id():
        raise ValueError("the process identity belongs to another machine")
    for field in ("pid", "start_ticks"):
        value = identity.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("invalid Windows process identity")
    return f"Local\\OrchestratorEngine.{identity['pid']}.{identity['start_ticks']}"


def active_processes(handle) -> int:
    info = Accounting()
    check(
        api().QueryInformationJobObject(
            handle,
            1,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
    )
    return info.active


def member_pids(handle) -> tuple[int, ...]:
    """Return the active process IDs owned by an already-open Job Object."""
    capacity = 16
    while True:
        buffer = ctypes.create_string_buffer(
            8 + ctypes.sizeof(ctypes.c_size_t) * capacity
        )
        if api().QueryInformationJobObject(handle, 3, buffer, len(buffer), None):
            count = ctypes.c_uint32.from_buffer(buffer, 4).value
            pids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
            return tuple(int(pid) for pid in pids)
        if ctypes.get_last_error() != 234:
            raise ctypes.WinError(ctypes.get_last_error())
        capacity = max(capacity * 2, ctypes.c_uint32.from_buffer(buffer).value)


def member_handles(handle) -> list:
    """Reserve process objects before termination; accounting alone is not exit."""
    handles = []
    try:
        for pid in member_pids(handle):
            process = api().OpenProcess(SYNCHRONIZE, False, pid)
            if process:
                handles.append(process)
            elif ctypes.get_last_error() != 87:
                raise ctypes.WinError(ctypes.get_last_error())
        return handles
    except BaseException:
        for process in handles:
            api().CloseHandle(process)
        raise


def job_member_state(identity: object, pid: object) -> str:
    """Prove whether a live PID belongs to the exact recorded Job Object."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "not_member"
    try:
        name = job_name(identity)
    except ValueError:
        return "unknown"
    handle = api().OpenJobObjectW(JOB_QUERY, False, name)
    if not handle:
        return "unknown"
    try:
        return "member" if pid in member_pids(handle) else "not_member"
    except OSError:
        return "unknown"
    finally:
        api().CloseHandle(handle)


def job_state(identity: object) -> str:
    try:
        name = job_name(identity)
    except ValueError:
        return "unknown"
    handle = api().OpenJobObjectW(JOB_QUERY, False, name)
    if not handle:
        return "gone" if ctypes.get_last_error() == 2 else "unknown"
    try:
        return "alive" if active_processes(handle) else "gone"
    except OSError:
        return "unknown"
    finally:
        api().CloseHandle(handle)


def terminate_job(identity: object, timeout: float = 10.0) -> bool:
    name = job_name(identity)
    handle = api().OpenJobObjectW(JOB_QUERY | JOB_TERMINATE, False, name)
    if not handle:
        if ctypes.get_last_error() == 2:
            return True
        raise ctypes.WinError(ctypes.get_last_error())
    members = []
    try:
        members = member_handles(handle)
        check(api().TerminateJobObject(handle, 1))
        deadline = time.monotonic() + timeout
        while active_processes(handle):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        for process in members:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if api().WaitForSingleObject(process, remaining) != 0:
                return False
        return True
    finally:
        for process in members:
            api().CloseHandle(process)
        api().CloseHandle(handle)


class JobProcess(subprocess.Popen):
    """Popen-compatible launcher, including pipes, cwd, environment and exit code."""

    def __init__(self, args, *, owned: bool = False, **kwargs):
        import msvcrt

        if isinstance(args, (str, bytes)) or kwargs.get("shell"):
            raise ValueError("managed Windows commands require an argv sequence")
        if kwargs.get("executable") or kwargs.get("startupinfo"):
            raise ValueError("custom executable/startupinfo is not supported")
        self._job = None
        self._owned = owned
        self.runtime_identity = None
        kwargs.pop("start_new_session", None)
        kwargs.pop("process_group", None)
        creationflags = kwargs.get("creationflags", 0)
        incompatible = creationflags & (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE
        )
        if incompatible:
            raise ValueError(
                "managed Windows commands cannot use DETACHED_PROCESS or "
                "CREATE_NEW_CONSOLE; JobProcess owns detachment and console hiding"
            )
        # No visible console is created for internal launchers or commands.
        kwargs["creationflags"] = creationflags | subprocess.CREATE_NO_WINDOW
        read_fd, write_fd = os.pipe()
        try:
            os.set_inheritable(read_fd, True)
            startup = subprocess.STARTUPINFO()
            handle = msvcrt.get_osfhandle(read_fd)
            startup.lpAttributeList = {"handle_list": [handle]}
            kwargs["startupinfo"] = startup
            kwargs["close_fds"] = True
            command = [
                # A Windows venv executable is a redirector which can fork
                # before job assignment. Launch the barrier with the base
                # interpreter; the requested argv still uses its own venv.
                getattr(sys, "_base_executable", sys.executable),
                str(Path(__file__).with_name("windows_child.py")),
                str(handle),
                *map(os.fspath, args),
            ]
            super().__init__(command, **kwargs)
            try:
                identity = identity_from_handle(int(self._handle), self.pid)
                self.runtime_identity = identity
                job = check(api().CreateJobObjectW(None, job_name(identity)))
                if ctypes.get_last_error() == 183:
                    api().CloseHandle(job)
                    raise OSError("process job already exists")
                self._job = job
                limits = ExtendedLimits()
                limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
                check(
                    api().SetInformationJobObject(
                        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                    )
                )
                check(api().AssignProcessToJobObject(job, int(self._handle)))
                # Duplicate before admission so dispatcher exit cannot race
                # the launcher's acquisition of a durable job handle.
                retained = w.HANDLE()
                if not owned:
                    check(
                        api().DuplicateHandle(
                            api().GetCurrentProcess(),
                            job,
                            int(self._handle),
                            ctypes.byref(retained),
                            0,
                            False,
                            2,
                        )
                    )
                os.write(write_fd, f"G {retained.value or 0}\n".encode("ascii"))
            except BaseException:
                # Closing the barrier prevents execution even before assignment.
                super().kill()
                super().wait(timeout=5)
                self.close_job()
                raise
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def close_job(self):
        if self._job is not None:
            handle, self._job = self._job, None
            try:
                if not terminate_job(self.runtime_identity):
                    raise OSError("process job did not terminate")
            finally:
                api().CloseHandle(handle)

    def poll(self):
        result = super().poll()
        if result is not None:
            self.close_job()
        return result

    def wait(self, timeout=None):
        result = super().wait(timeout=timeout)
        self.close_job()
        return result

    def __del__(self):
        # Closing owned jobs also contains unexpected supervisor exceptions.
        if getattr(self, "_job", None) is not None:
            api().CloseHandle(self._job)
            self._job = None
        super().__del__()
