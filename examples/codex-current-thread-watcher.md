# Codex headless history delivery example

> Legacy invocation retained for compatibility. New setups should use durable
> inbox history and manual acknowledgement as documented in
> [docs/setup-guide.md](../docs/setup-guide.md).

This example submits a follow-up turn through a headless Codex App Server when
a worker writes a terminal event. It can write the turn to thread history, but
it does not reliably refresh or wake an already-open Codex Desktop chat.

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
orchestrator-engine --project-root /path/to/project watcher service status
```
