# Project adoption

For the standard end-to-end setup (install, bind, workers, watcher, smoke
test) follow [setup-guide.md](setup-guide.md). This document covers the
adoption contract itself and bridging legacy layouts.

For a new project, start with the create-only scaffolder:
`orchestrator-engine --project-root /path/to/project adopt`. This document is
about deeper project integration and legacy bridges after that local layout
exists.

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

Once a project writes standard signals, bind the host chat and select its
documented delivery mechanism:

```bash
orchestrator-engine --project-root /path/to/project bind \
  --host codex --thread-id THREAD_ID
```

For Codex, use durable history and explicit manual acknowledgement instead of
starting a callback watcher for live refresh. See [hosts.md](hosts.md) for
Claude and VS Code hosts. The legacy invocation
`--action current-thread-callback --target-thread-id THREAD_ID` remains
supported for headless history delivery only.

For engine-version upgrades and watcher-state migration, see
[upgrade-guide.md](upgrade-guide.md).
