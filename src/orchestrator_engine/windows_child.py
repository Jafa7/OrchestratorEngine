"""Internal Windows launch barrier; never execute user work before admission.

The parent assigns this process to a Job Object, then releases the inherited
pipe. EOF means the parent failed before admission: exit without launching.
This module deliberately has no package imports and can run from a wheel path.
"""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    import msvcrt

    handle = int(sys.argv[1])
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    with os.fdopen(descriptor, "rb", buffering=0) as gate:
        admission = gate.readline(128).split()
        if len(admission) != 2 or admission[0] != b"G":
            return 125
    # A non-inheritable job handle was duplicated before admission. Keeping
    # it until launcher exit lets a detached dispatcher leave independently.
    retained_job = int(admission[1])
    try:
        return subprocess.call(sys.argv[2:])
    except OSError as error:
        print(f"orchestrator command launch failed: {error}", file=sys.stderr)
        return 127
    finally:
        if retained_job:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            kernel.CloseHandle(retained_job)


if __name__ == "__main__":
    raise SystemExit(main())
