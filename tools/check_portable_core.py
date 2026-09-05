#!/usr/bin/env python3
"""Smoke-test the installed package's cross-platform core boundary."""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import orchestrator_engine
from orchestrator_engine import platform_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expect-detached",
        choices=("supported", "unsupported"),
        required=True,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for module in pkgutil.iter_modules(
        orchestrator_engine.__path__, orchestrator_engine.__name__ + "."
    ):
        importlib.import_module(module.name)

    capabilities = platform_runtime.capabilities()
    assert capabilities["portable_core"] == "supported"
    assert capabilities["file_locking"] == "supported"
    assert capabilities["detached_lifecycle"] == args.expect_detached

    with tempfile.TemporaryDirectory() as temporary:
        lock_path = Path(temporary) / "portable-core.lock"
        with platform_runtime.exclusive_file_lock(lock_path):
            assert lock_path.is_file()

        holder_code = (
            "import sys,time; from pathlib import Path; "
            "from orchestrator_engine import platform_runtime as p; "
            "lock=p.exclusive_file_lock(Path(sys.argv[1])); lock.__enter__(); "
            "print('locked', flush=True); time.sleep(0.75); "
            "lock.__exit__(None,None,None)"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "locked"
            started = time.monotonic()
            with platform_runtime.exclusive_file_lock(lock_path):
                pass
            assert time.monotonic() - started >= 0.2
            assert holder.wait(timeout=5) == 0
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.wait(timeout=5)
            if holder.stdout is not None:
                holder.stdout.close()

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert platform_runtime.process_alive(process.pid)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)

    print(
        "portable core: ok; detached lifecycle: "
        f"{capabilities['detached_lifecycle']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
