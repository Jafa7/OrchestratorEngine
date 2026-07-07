# Project adoption

Projects integrate with OrchestratorEngine by implementing its standard file
contract. OrchestratorEngine should not grow project-specific layouts.

## Required project behavior

A project that wants orchestration support should write:

```text
.orchestrator/
  events/<event_id>.json
  inbox/signals/<event_id>.json
```

The event and signal schemas are defined in [contracts.md](contracts.md).

## Legacy projects

If a project already has a private orchestration layout, add a project-local
bridge that converts legacy events into the standard `.orchestrator` contract.

Recommended bridge behavior:

- copy or project one terminal event to `.orchestrator/events/<event_id>.json`;
- write a standard `LOCAL_AI_WORKER_FINISHED` signal to
  `.orchestrator/inbox/signals/<event_id>.json`;
- preserve legacy source paths as metadata;
- migrate already-seen event IDs into `.orchestrator/inbox/watcher-state.json`
  during adoption so historical events are not re-dispatched;
- keep the bridge in the project repository, not in OrchestratorEngine core.

## Watcher ownership

Once a project writes standard signals, start the watcher against the project
root:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --action current-thread-callback \
  --target-thread-id THREAD_ID \
  service start --interval-seconds 5
```

