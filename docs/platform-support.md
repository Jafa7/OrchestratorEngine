# Platform support

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
| Native Windows | Supported | Supported for portable configured commands | Not currently supported; use WSL or Linux |
| macOS | Supported | Supported for portable configured commands | Not currently supported; use Linux |

The portable core includes package import, schemas, immutable JSON contracts,
read-only capability reports, bounded status inspection and cross-process
advisory locks. Native Windows is exercised directly during development and
Windows and macOS portable-core imports and locking are checked in CI. Windows
lock acquisition follows the standard-library `msvcrt` bounded wait; failure
to acquire the lock is reported instead of proceeding without exclusivity.
Process inspection on Windows opens a query handle and never uses POSIX-style
signal zero. Reaper commands remain unavailable outside Linux because an
unreadable Linux process identity is `unknown`, not evidence that a supervisor
has exited.

Detached lifecycle support currently requires Linux `/proc` process identity
and POSIX process-group behavior. The engine does not silently substitute a
weaker process model on another operating system because doing so could report
a recycled process as the original supervisor or leave descendants running.
This is an implementation boundary, not provider-specific policy.

Host delivery may cross that boundary through platform interop. For example,
an engine running in WSL can invoke the Windows Codex or VS Code CLI while
retaining Linux lifecycle guarantees for its local watcher and workers. See
[Host setup](hosts.md) for delivery-specific requirements and
[External tool prerequisites](external-tools.md) for adopter-owned CLIs.
