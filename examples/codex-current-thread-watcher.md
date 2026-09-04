# Codex live task delivery example

Current Codex Desktop releases expose a session queue on the local CLI. Confirm
the capability, bind from the task that should receive completions, and start a
host-scoped watcher:

```bash
codex queue --help
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
