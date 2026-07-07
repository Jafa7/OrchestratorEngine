# Codex current-thread watcher example

This example starts a watcher that wakes an existing Codex Desktop thread when a
worker writes a terminal event.

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
