"""Darwin process identity using the public libproc BSD process record.

The ABI is defined in Apple's bsd/sys/proc_info.h (PROC_PIDTBSDINFO).
No shell parsing, process-table cache or floating-point timestamps are used.
"""

from __future__ import annotations

import ctypes
import errno
from functools import lru_cache


class BSDInfo(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in (
            "flags",
            "status",
            "xstatus",
            "pid",
            "ppid",
            "uid",
            "gid",
            "ruid",
            "rgid",
            "svuid",
            "svgid",
            "rfu",
        )
    ] + [
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


@lru_cache(maxsize=1)
def library():
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.proc_pidinfo.restype = ctypes.c_int
    return lib


@lru_cache(maxsize=1)
def system_library():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.sysctlbyname.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    lib.sysctlbyname.restype = ctypes.c_int
    return lib


def boot_id() -> str | None:
    value = ctypes.create_string_buffer(128)
    size = ctypes.c_size_t(len(value))
    if (
        system_library().sysctlbyname(
            b"kern.bootsessionuuid", value, ctypes.byref(size), None, 0
        )
        != 0
    ):
        return None
    return value.value.decode("ascii") or None


def probe(pid: int) -> dict:
    info = BSDInfo()
    ctypes.set_errno(0)
    size = library().proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        state = "gone" if size == 0 and ctypes.get_errno() == errno.ESRCH else "unknown"
        return {"state": state, "identity": None}
    boot = boot_id()
    if info.pid != pid or not boot or info.start_sec == 0:
        return {"state": "unknown", "identity": None}
    return {
        "state": "present",
        "identity": {
            "source": "darwin-proc-bsdinfo",
            "pid": pid,
            "start_ticks": info.start_sec * 1_000_000 + info.start_usec,
            "boot_id": boot,
            "state": "Z" if info.status == 5 else "R",
        },
    }
