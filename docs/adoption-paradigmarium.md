# Paradigmarium adoption

Paradigmarium already has a private orchestration layout:

```text
.paradigmarium/orchestration/
  supervisor/events/
  orchestrator-inbox/signals/
  orchestrator-inbox/thread-wakeups/
  orchestrator-inbox/logs/
```

Use `--layout paradigmarium` to read and write that layout without copying
OrchestratorEngine code into Paradigmarium.

## Wrapper

Paradigmarium can provide a thin wrapper:

```bash
plan/tools/orchestrator_engine.sh --project-root /home/user/Project/DocumentationEngine \
  --layout paradigmarium watcher service status
```

The wrapper should locate the local OrchestratorEngine checkout or an installed
`orchestrator-engine` command. It must not duplicate core watcher logic.

## Migration strategy

1. Keep the existing Paradigmarium watcher as a fallback.
2. Run OrchestratorEngine in `record` mode against real inbox signals.
3. Verify state path, heartbeat and retention behavior.
4. Switch current-thread wakeup service to OrchestratorEngine.
5. Retire the project-local watcher only after live event delivery is proven.

