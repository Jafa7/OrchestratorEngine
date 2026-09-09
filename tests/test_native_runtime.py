"""Native lifecycle acceptance shared by the macOS and Windows packages."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_engine import (
    core,
    github_actions,
    local_checks,
    platform_runtime,
    runtime_macos,
    watcher,
    worker_lease,
    workers,
)


def read_line(process) -> str:
    output = queue.Queue()
    thread = threading.Thread(
        target=lambda: output.put(process.stdout.readline()), daemon=True
    )
    thread.start()
    return output.get(timeout=10)


class NativeRuntimeTests(unittest.TestCase):
    def test_ci_capture_timeout_uses_native_cleanup(self):
        result = github_actions.run_bounded_command(
            [
                sys.executable,
                "-c",
                "import time; print('started',flush=True); time.sleep(30)",
            ],
            timeout_seconds=0.2,
        )
        self.assertTrue(result["timed_out"])

    def test_atomic_state_survives_concurrent_readers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            core.atomic_json(path, {"revision": 0})
            errors = []
            stop = threading.Event()

            def read():
                try:
                    while not stop.is_set():
                        self.assertIsInstance(core.load_object(path)["revision"], int)
                except Exception as error:
                    errors.append(error)

            reader = threading.Thread(target=read)
            reader.start()
            try:
                for revision in range(1, 51):
                    core.atomic_json(path, {"revision": revision})
            finally:
                stop.set()
                reader.join(timeout=5)
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(core.load_object(path), {"revision": 50})

    def test_current_identity_rejects_pid_reuse_and_foreign_backend(self):
        identity = worker_lease.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertEqual(worker_lease.identity_state(identity)["state"], "alive")
        changed = {**identity, "start_ticks": identity["start_ticks"] + 1}
        self.assertEqual(worker_lease.identity_state(changed)["state"], "gone")
        foreign = {**identity, "source": "foreign-runtime"}
        self.assertEqual(worker_lease.identity_state(foreign)["state"], "unknown")
        self.assertFalse(worker_lease.identity_matches(foreign, identity))
        incomplete = {
            key: value for key, value in identity.items() if key != "start_ticks"
        }
        self.assertEqual(worker_lease.identity_state(incomplete)["state"], "unknown")

    def test_command_preserves_arguments_stdin_cwd_environment_and_exit_code(self):
        with tempfile.TemporaryDirectory(prefix="native runtime ") as temporary:
            process = platform_runtime.spawn(
                subprocess.Popen,
                [
                    sys.executable,
                    "-c",
                    "import os,sys,json; "
                    "print(json.dumps([sys.argv[1:],sys.stdin.read(),os.getcwd(),"
                    "os.environ['OE_NATIVE_TEST']])); sys.exit(7)",
                    "space value",
                    'a"b',
                    "текст",
                    "a&b",
                ],
                cwd=temporary,
                env={**os.environ, "OE_NATIVE_TEST": "present"},
                owned=True,
                start_new_session=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            output, error = process.communicate("input payload", timeout=10)
            self.assertEqual(process.returncode, 7, error)
            args, received, cwd, environment = json.loads(output)
            self.assertEqual(args, ["space value", 'a"b', "текст", "a&b"])
            self.assertEqual(received, "input payload")
            self.assertEqual(Path(cwd).resolve(), Path(temporary).resolve())
            self.assertEqual(environment, "present")

    def test_owner_stops_descendants_and_preserves_unrelated_process(self):
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        process = platform_runtime.spawn(
            subprocess.Popen,
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time; "
                "p=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)']); "
                "print(p.pid,flush=True); time.sleep(30)",
            ],
            owned=True,
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            child_pid = int(read_line(process))
            child_identity = worker_lease.process_identity(child_pid)
            result = workers.terminate_worker(
                process,
                process_group=platform_runtime.process_group(process.pid),
                reason="native_acceptance",
                grace_seconds=0.1,
                timeout_seconds=3,
            )
            self.assertTrue(result["exited"])
            self.assertTrue(
                worker_lease.wait_until_gone(
                    child_pid,
                    child_identity,
                    timeout_seconds=3,
                    poll_seconds=0.02,
                )
            )
            self.assertIsNone(unrelated.poll())
        finally:
            if process.poll() is None:
                workers.force_terminate_worker(
                    process, process_group=platform_runtime.process_group(process.pid)
                )
            process.stdout.close()
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_check_foreground_and_detached_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / ".orchestrator" / "checks.toml"
            config.parent.mkdir()
            command = [sys.executable, "-c", "import time; time.sleep(30)"]
            config.write_text(
                "schema_version = 1\n[suites.native]\n"
                '[[suites.native.commands]]\nlabel = "timeout"\n'
                f"argv = {json.dumps(command)}\n"
                "timeout_seconds = 0.2\n",
                encoding="utf-8",
            )
            spawned = []
            spawn = platform_runtime.spawn

            def capture(*args, **kwargs):
                process = spawn(*args, **kwargs)
                spawned.append(process)
                return process

            self.addCleanup(lambda: [p.wait(timeout=5) for p in spawned])
            for mode in ("foreground", "detached"):
                with (
                    self.subTest(mode=mode),
                    mock.patch.object(platform_runtime, "spawn", side_effect=capture),
                ):
                    result = local_checks.start_check(
                        root,
                        check_id=f"NATIVE-{mode}",
                        suite="native",
                        execution=mode,
                        wake_policy="never",
                    )
                    path = Path(result["descriptor_path"])
                    deadline = time.monotonic() + 10
                    while core.load_object(path).get("status") in {
                        "starting",
                        "running",
                    }:
                        if time.monotonic() > deadline:
                            self.fail(f"check did not finish: {path}")
                        time.sleep(0.02)
                    descriptor = core.load_object(path)
                    self.assertIn(descriptor["status"], {"failed", "timed_out"})
                    self.assertNotIn("active_command", descriptor)
                    for process in spawned:
                        process.wait(timeout=5)

    def test_watcher_service_start_stop_is_native_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "service.json"
            spawned = []
            spawn = platform_runtime.spawn

            def capture(*args, **kwargs):
                process = spawn(*args, **kwargs)
                spawned.append(process)
                return process

            with mock.patch.object(platform_runtime, "spawn", side_effect=capture):
                state = watcher.start_service(
                    [root],
                    interval_seconds=0.1,
                    state_path=root / "watcher.json",
                    service_file=path,
                    action="notify",
                    target_thread_id=None,
                    codex="unused",
                )
            try:
                with mock.patch.object(platform_runtime, "spawn", side_effect=capture):
                    restarted = watcher.restart_service(
                        [root],
                        interval_seconds=None,
                        state_path=None,
                        service_file=path,
                        action=None,
                        target_thread_id=None,
                        codex="unused",
                        timeout_seconds=3,
                    )
                self.assertNotEqual(restarted["pid"], state["pid"])
                state = restarted
                stopped = watcher.stop_service(
                    [root], service_file=path, timeout_seconds=3
                )
                self.assertEqual(stopped["status"], "stopped")
                self.assertEqual(
                    watcher.stop_service([root], service_file=path)["status"], "stopped"
                )
            finally:
                identity = state["process_identity"]
                worker_lease.stop_worker_tree(
                    worker_pid=state["pid"],
                    worker_pgid=state["pid"],
                    worker_identity=identity,
                    reason="test_cleanup",
                    grace_seconds=0.1,
                )
                for process in spawned:
                    process.wait(timeout=5)


class MacOSIdentityTests(unittest.TestCase):
    def test_public_bsd_record_layout_and_integer_timestamp(self):
        self.assertEqual(ctypes.sizeof(runtime_macos.BSDInfo), 136)
        self.assertEqual(runtime_macos.BSDInfo.start_sec.offset, 120)

        def fill(pid, flavor, arg, target, size):
            info = ctypes.cast(target, ctypes.POINTER(runtime_macos.BSDInfo)).contents
            info.pid, info.status = pid, 5
            info.start_sec, info.start_usec = 1234567890, 987654
            return size

        library = mock.Mock()
        library.proc_pidinfo.side_effect = fill
        with (
            mock.patch.object(runtime_macos, "library", return_value=library),
            mock.patch.object(runtime_macos, "boot_id", return_value="boot-test"),
        ):
            identity = runtime_macos.probe(123)["identity"]
        self.assertEqual(identity["start_ticks"], 1234567890987654)
        self.assertEqual(identity["state"], "Z")

    def test_missing_denied_and_truncated_records_are_distinct(self):
        for error, size, expected in [
            (errno.ESRCH, 0, "gone"),
            (errno.EPERM, 0, "unknown"),
            (0, 32, "unknown"),
        ]:

            def fail(*args):
                ctypes.set_errno(error)
                return size

            lib = mock.Mock()
            lib.proc_pidinfo.side_effect = fail
            with mock.patch.object(runtime_macos, "library", return_value=lib):
                self.assertEqual(runtime_macos.probe(123)["state"], expected)

    def test_python_without_waitid_preserves_zombie_reservation(self):
        with (
            mock.patch.object(workers, "os", wraps=os) as wrapped,
            mock.patch.object(
                worker_lease,
                "process_identity_probe",
                return_value={
                    "state": "present",
                    "identity": {"state": "Z"},
                },
            ),
        ):
            del wrapped.waitid
            self.assertTrue(
                workers.wait_for_exit(123, timeout_seconds=0, poll_seconds=0)
            )


@unittest.skipUnless(os.name == "nt", "requires native Windows Job Objects")
class WindowsJobTests(unittest.TestCase):
    def test_venv_redirected_watcher_heartbeat_is_owned_and_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            spawned = []
            spawn = platform_runtime.spawn

            def capture(*args, **kwargs):
                process = spawn(*args, **kwargs)
                spawned.append(process)
                return process

            with mock.patch.object(platform_runtime, "spawn", side_effect=capture):
                started = watcher.start_service(
                    [root],
                    interval_seconds=0.05,
                    state_path=None,
                    service_file=None,
                    action="callback",
                    target_thread_id=None,
                    codex=sys.executable,
                    host="codex",
                )
            service_path = Path(started["service_file"])
            deadline = time.monotonic() + 10
            status = None
            try:
                while time.monotonic() < deadline:
                    status = watcher.service_status([root], host="codex")
                    if status["status"] == "running":
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(status)
                self.assertEqual(status["status"], "running", status)
                self.assertNotEqual(status["heartbeat_pid"], started["pid"])
                self.assertEqual(
                    status["heartbeat_process_membership"], "member"
                )
            finally:
                stopped = watcher.stop_service(
                    [root], service_file=service_path, host="codex"
                )
                for process in spawned:
                    process.wait(timeout=5)
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(
                worker_lease.identity_state(started["process_identity"])["state"],
                "gone",
            )

    def test_delayed_assignment_contains_the_executing_python_process(self):
        from ctypes import wintypes

        from orchestrator_engine import runtime_windows

        assign = runtime_windows.api().AssignProcessToJobObject

        def delayed(job, handle):
            # Expose a venv redirector's opportunity to fork before assignment.
            time.sleep(0.2)
            return assign(job, handle)

        with mock.patch.object(
            runtime_windows.api(), "AssignProcessToJobObject", side_effect=delayed
        ):
            process = platform_runtime.spawn(
                subprocess.Popen,
                [
                    sys.executable,
                    "-c",
                    "import os,time; print(os.getpid(),flush=True); time.sleep(30)",
                ],
                owned=True,
                stdout=subprocess.PIPE,
                text=True,
            )
        pid = int(read_line(process))
        handle = runtime_windows.check(
            runtime_windows.api().OpenProcess(
                runtime_windows.QUERY | runtime_windows.SYNCHRONIZE,
                False,
                pid,
            )
        )
        try:
            contained = wintypes.BOOL()
            runtime_windows.check(
                runtime_windows.api().IsProcessInJob(
                    handle,
                    process._job,
                    ctypes.byref(contained),
                )
            )
            self.assertTrue(contained.value, "executing Python escaped its job")
        finally:
            # The reserved handle prevents PID reuse during failure cleanup.
            if runtime_windows.api().WaitForSingleObject(handle, 0) == 258:
                os.kill(pid, 15)
            platform_runtime.stop_owned(process, reason="test_cleanup")
            runtime_windows.api().CloseHandle(handle)
            process.stdout.close()

    def test_unavailable_and_foreign_job_identity_fail_closed(self):
        from orchestrator_engine import runtime_windows

        identity = worker_lease.process_identity(os.getpid())
        identity["machine_id"] = "0" * 64
        self.assertEqual(worker_lease.identity_state(identity)["state"], "unknown")
        with self.assertRaises(ValueError):
            runtime_windows.terminate_job(identity)
        with (
            mock.patch.object(runtime_windows.api(), "OpenJobObjectW", return_value=0),
            mock.patch.object(ctypes, "get_last_error", return_value=5),
        ):
            local = worker_lease.process_identity(os.getpid())
            self.assertEqual(runtime_windows.job_state(local), "unknown")
            with self.assertRaises(OSError):
                runtime_windows.terminate_job(local)

    def test_launch_barrier_eof_never_executes_user_command(self):
        import msvcrt

        from orchestrator_engine import windows_child

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "must-not-exist"
            read_fd, write_fd = os.pipe()
            try:
                os.set_inheritable(read_fd, True)
                handle = msvcrt.get_osfhandle(read_fd)
                startup = subprocess.STARTUPINFO()
                startup.lpAttributeList = {"handle_list": [handle]}
                child = subprocess.Popen(
                    [
                        sys.executable,
                        windows_child.__file__,
                        str(handle),
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(target)!r}).touch()",
                    ],
                    startupinfo=startup,
                    close_fds=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            finally:
                os.close(read_fd)
                os.close(write_fd)
            self.assertEqual(child.wait(timeout=5), 125)
            self.assertFalse(target.exists())

    def test_leader_exit_does_not_leave_unmanaged_descendants(self):
        from orchestrator_engine import runtime_windows

        process = platform_runtime.spawn(
            subprocess.Popen,
            [
                sys.executable,
                "-c",
                "import subprocess,sys; "
                "p=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)']); "
                "print(p.pid,flush=True)",
            ],
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        identity = process.runtime_identity
        try:
            child_pid = int(read_line(process))
            process.wait(timeout=5)
            self.assertEqual(runtime_windows.job_state(identity), "gone")
            wrong = {**identity, "start_ticks": identity["start_ticks"] + 1}
            self.assertTrue(runtime_windows.terminate_job(wrong))
            self.assertFalse(platform_runtime.process_alive(child_pid))
            self.assertTrue(runtime_windows.terminate_job(identity))
            self.assertEqual(runtime_windows.job_state(identity), "gone")
        finally:
            runtime_windows.terminate_job(identity)
            process.stdout.close()

    def test_owner_crash_kills_owned_job_but_detached_job_survives(self):
        from orchestrator_engine import runtime_windows

        for owned in (True, False):
            code = (
                "import os,sys,json,subprocess; "
                "from orchestrator_engine import platform_runtime; "
                "p=platform_runtime.spawn(subprocess.Popen,"
                "[sys.executable,'-c','import time; time.sleep(30)'],"
                f"owned={owned},start_new_session=True); "
                "print(json.dumps(p.runtime_identity),flush=True); os._exit(0)"
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
            )
            identity = json.loads(read_line(parent))
            try:
                parent.wait(timeout=5)
                deadline = time.monotonic() + 5
                while owned and runtime_windows.job_state(identity) != "gone":
                    if time.monotonic() >= deadline:
                        self.fail("owned job survived controller crash")
                    time.sleep(0.02)
                self.assertEqual(
                    runtime_windows.job_state(identity), "gone" if owned else "alive"
                )
            finally:
                runtime_windows.terminate_job(identity)
                parent.stdout.close()

    def test_failed_assignment_never_executes_user_command(self):
        from orchestrator_engine import runtime_windows

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "must-not-exist"
            command = [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(target)!r}).touch()",
            ]
            with (
                mock.patch.object(
                    runtime_windows.api(), "AssignProcessToJobObject", return_value=0
                ),
                self.assertRaises(OSError),
            ):
                platform_runtime.spawn(subprocess.Popen, command, owned=True)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
