# Codex live task delivery example

Current Codex Desktop releases expose a session queue on the local CLI. Confirm
the capability, bind from the task that should receive completions, and start a
host-scoped watcher:

```bash
codex queue --help
orchestrator-engine --project-root /path/to/project \
  codex diagnose --timeout-seconds 10
orchestrator-engine --project-root /path/to/project bind --host codex
orchestrator-engine --project-root /path/to/project watcher \
  --host codex --action callback service start --interval-seconds 5
```

The queue serializes a completion message behind any active turn in the target
task. Older CLIs automatically use the durable headless App Server fallback.

## Legacy explicit target

The historical current-thread action remains available when an operator needs
to select the target id directly:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --action current-thread-callback \
  --target-thread-id THREAD_ID \
  service start --interval-seconds 5
```

Create a sample event:

```bash
orchestrator-engine --project-root /path/to/project emit \
  --task-id TASK-001 \
  --terminal-status completed \
  --result /path/to/project/result.json \
  --evidence /path/to/project/evidence.json
```

Check watcher health:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host codex service status
```

WSL and OS restarts stop the CLI-managed detached watcher process; the engine
does not install or recreate an OS-level daemon. After a restart, run the
health command above. If it reports `not_started`, start the service again:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host codex --action callback service start --interval-seconds 5
```

If it reports `crashed` or `stopped`, use `service restart` with the same host
scope. Do not run `codex migrate-rollouts --apply` as watcher recovery.
