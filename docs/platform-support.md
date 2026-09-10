# Platform support

The table below describes v1.9.1. Native macOS and Windows lifecycle backends
join Linux/WSL support. See [Native runtime packages](native-runtime-packages.md)
for process containment boundaries and the exact-candidate CI acceptance gates.

OrchestratorEngine separates its portable data/CLI core from the process
lifecycle guarantees required by detached workers, monitors and watcher
services. Check the current machine before adoption:

```bash
orchestrator-engine runtime-capabilities
```

The report is machine-readable and fail-safe. A platform marked as limited can
still inspect contracts and run supported foreground operations, but detached
commands reject the request before creating task or service artifacts.

| Runtime | Portable core and file locks | Foreground local checks | Detached workers, checks, monitors and watcher services |
| --- | --- | --- | --- |
| Linux | Supported | Supported | Supported |
| Windows with WSL | Supported inside WSL | Supported inside WSL | Supported inside WSL |
| Native Windows | Supported | Supported for portable configured commands | Supported with native Job Objects |
| macOS | Supported | Supported for portable configured commands | Supported with native POSIX groups |

The hosted native baseline is Windows Server 2022 x64 and macOS 15 on Intel and
Apple Silicon. Other editions, architectures and OS releases retain the same
contract only after exact-environment field verification.

The portable core includes package import, schemas, immutable JSON contracts,
read-only capability reports, bounded status inspection and cross-process
advisory locks. Native Windows is exercised directly during development and
Windows and macOS portable-core imports and locking are checked in CI. Windows
lock acquisition follows the standard-library `msvcrt` bounded wait; failure
to acquire the lock is reported instead of proceeding without exclusivity.
Process inspection uses native kernel creation identities: Linux process-stat
ticks and boot ID, macOS BSD process start time and boot-session UUID, and
Windows creation FILETIME and machine identity. Unavailable identity is unknown,
not evidence of exit. Native Windows termination uses Job Objects; Linux and
macOS use POSIX groups. A Windows watcher heartbeat from a virtual-environment
redirector descendant is accepted only after the runtime proves membership in
the service's exact recorded Job Object. Deliberately escaped POSIX groups and cross-OS process
bridges require separate adopter containment contracts.

Host delivery may cross that boundary through platform interop. For example,
an engine running in WSL can invoke the Windows Codex or VS Code CLI while
retaining Linux lifecycle guarantees for its local watcher and workers. See
[Host setup](hosts.md) for delivery-specific requirements and
[External tool prerequisites](external-tools.md) for adopter-owned CLIs.

## Desktop host boundary

Native runtime support does not by itself certify a desktop application's live
chat behavior. The macOS CI runners have no interactive Codex Desktop session,
so they verify CLI/runtime lifecycle and durable delivery contracts but do not
claim that a particular Desktop and CLI version visibly resumes a live chat.
Login/logout, sleep/resume, reboot persistence and app-update behavior remain
field checks for the exact host versions. Record an untested field as
`not_tested`, never as inferred support. See
[Native acceptance](native-acceptance.md).

Hosted CI covers local runner storage. OneDrive, SMB/NFS, external volumes,
case-sensitive APFS and Windows/WSL shared paths are separate field-test
topologies. Security products and OS policy can also alter process, file and
background-app behavior. A successful local-filesystem CI job must not be used
as evidence for those environments.
