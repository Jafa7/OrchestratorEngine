# Native runtime implementation packages

Version 1.6.0 adds these two integration packages to the Linux/WSL runtime
shipped in v1.5.0. Earlier release assets remain unchanged.
macOS acceptance requires the native GitHub Actions jobs to pass for the exact
candidate; a Linux mock result is not macOS acceptance evidence.

## Release verification

[v1.6.0](https://github.com/Jafa7/OrchestratorEngine/releases/tag/v1.6.0) is
published from commit `85b5dea6fb10c8bfb9ef2a1d3c88e3b8eaca83e9`.
Its [exact-commit CI run](https://github.com/Jafa7/OrchestratorEngine/actions/runs/34330574701)
passed all 14 jobs, including native lifecycle, resource coordination and full
conformance on macOS Intel/ARM and Windows with Python 3.11/3.13, plus Linux,
portable-core, packaging and historical upgrade checks.
The [release workflow](https://github.com/Jafa7/OrchestratorEngine/actions/runs/34330794883)
verified provenance, installed-wheel conformance and uploaded asset digests
before publishing the wheel, sdist and `SHA256SUMS`.

This evidence covers the synthetic native contracts below. Project-specific
commands, desktop delivery and bridge topologies retain their separate
acceptance requirements. Later candidates require their own exact-commit CI.

## Shared contract

The same worker, check, monitor and watcher commands use a native process
boundary. Durable operations, wake policies, result formats and project policy
remain shared. Docker, a database and any particular worker provider are not
required. No new runtime dependency is installed.

Process identities include a backend discriminator and integer kernel creation
token. Linux retains its existing process-stat ticks and boot ID. macOS records
microseconds from the BSD process record and a boot-session UUID. Windows uses
the process creation FILETIME and a hashed machine identifier. Tokens from a
different backend or Windows machine are unknown, not proof of process exit.
The legacy `worker_pgid`/`process_group` integer identifies the native group
leader; on Windows it is meaningful only with its full recorded identity.

Checks distinguish an absent process from denied or unavailable identity data.
Cancellation and recovery must not signal unrelated processes after PID reuse.
Schema-version-1 Linux leases remain readable; the lease schema additionally
accepts the two native identity forms. Older Linux-only engines must not manage
native runtime state. Stop native services with the version that created them
before downgrading.

## Package MACOS: native POSIX lifecycle

- `runtime_macos.py` reads `PROC_PIDTBSDINFO` through libproc and
  `kern.bootsessionuuid` through sysctl. It checks ABI record size, process ID,
  boot identity and integer timestamps; denied and truncated reads fail closed.
- Existing POSIX groups provide launch, cancellation and timeout handling.
  On Python 3.11/3.12, process inspection waits for exit without reaping the
  child, preserving its PID reservation for the parent's final group sweep.
- The shared integration points cover workers, foreground/detached checks,
  CI/PR monitors and watcher service start/stop.
- The `macos-runtime` CI job installs the package and runs native acceptance
  plus full synthetic conformance from the candidate wheel on Intel and Apple
  Silicon. Python 3.11/3.13 run on both architectures, while 3.12 runs on Apple
  Silicon. Each combination records a bounded five-iteration acceptance soak
  as a retained CI artifact. It runs no model CLI and requires no provider
  credentials.

This retains the POSIX containment boundary: a deliberately daemonized process
that leaves its group is outside that group. An external reaper cannot safely
sweep an unverifiable, recycled group. Unknown cleanup must remain visible;
this package does not claim container-like isolation or resource reservation.

## Package WINDOWS: native Job Object lifecycle

- `runtime_windows.py` uses process handles and creation FILETIME, named Job
  Objects and explicit process-object waits. No POSIX signal or PID-only
  `taskkill` is used for managed Windows process trees.
- An internal Python launcher waits on an inherited admission pipe. The owner
  assigns the launcher to its job before allowing it to execute the configured
  argv. EOF or assignment failure prevents user command execution.
- All jobs use kill-on-close and disable breakaway. For an owned command, the
  supervisor owns the job handle. For a detached supervisor, a non-inheritable
  handle is duplicated into the launcher before admission so dispatch can exit
  safely. Nested commands get their own jobs inside the supervisor job.
- Termination uses the recorded job identity, reserves member process handles,
  terminates the job and checks exit. The ledger reports `TerminateJobObject`;
  native Windows does not promise a POSIX SIGTERM grace interval. Applications
  needing cooperative shutdown require their own explicit command protocol.
- argv, stdin, output, cwd, environment and command exit code are preserved.
  Commands must be argv sequences; use an explicit shell executable when the
  configured command needs shell syntax. Launchers create no visible console.
- Known transient Windows file-sharing errors use bounded retries of the same
  atomic rename/read. Native readers allow delete sharing, and per-path kernel
  mutexes serialize reads with replacement without creating read-side files.
  Permanent errors propagate; destination files are never removed to emulate
  replacement. Kernel object names are local to the Windows login session;
  this does not certify cross-session service or cross-OS shared-state access.
- The `windows-runtime` CI job installs the package and runs native lifecycle,
  controller-crash, launch-barrier and full synthetic conformance checks on
  Python 3.11, 3.12 and 3.13 from the candidate wheel. It retains the same
  bounded native acceptance artifact as macOS. Existing Linux tests continue
  to protect the shared core.

Windows Job Objects require compatible nested-job support. If the enclosing
host prohibits job assignment, launch fails before user work starts. Running
inside Windows from a WSL-owned process is a separate interoperability scenario;
passing these native jobs does not certify every WSL/Windows process bridge.

## Acceptance and release

Each package includes its adapter, all dependent call sites, tests, schema and
documentation changes. Run focused native checks while implementing, then the
full Linux repository gate on the finished combined candidate. Both native CI
jobs must pass on that same commit before publishing support for both systems.
Keep a failed or unrun OS job explicit in the handoff.

Acceptance covers identity mismatch and unavailable identity, command I/O,
descendant cleanup, timeout, watcher restart/stop and synthetic worker recovery.
Windows additionally covers owner death, detached survival and denied job
assignment. Desktop host delivery, login/logout, sleep/resume, reboot and
external tool availability need adopter/environment testing. Watchers remain
CLI-managed processes; neither package installs an OS startup service.

Native Windows and macOS machines can check the installed candidate from an
exact-tag checkout with:

```bash
python tools/run_native_acceptance.py \
  --cli "$(command -v orchestrator-engine)" \
  --expected-system Darwin \
  --expected-machine "$(uname -m)" \
  --output native-acceptance.json
```

The PowerShell equivalent is documented in
[Native acceptance](native-acceptance.md).
The runtime capability report describes available mechanisms on the current
machine. It does not replace the exact-candidate release evidence. See
[Native acceptance](native-acceptance.md) for the report contract and the
separate interactive Desktop field check.
