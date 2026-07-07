# OrchestratorEngine

OrchestratorEngine is a small event-driven coordination layer for AI worker
processes. It is designed for workflows where a worker runs outside the active
orchestrator turn, writes a terminal event to disk, and a local watcher wakes the
orchestrator only when there is real work to review.

The initial target is Codex Desktop current-thread wakeup without API keys or
model polling.

## Goals

- Run workers detached from the active orchestrator turn.
- Store terminal events and inbox signals as durable JSON files.
- Wake an orchestrator thread with a bounded pointer to event/evidence/result.
- Avoid token-spending heartbeat prompts.
- Keep provider integrations at explicit adapter boundaries.
- Provide service-style watcher control: start, status, stop and restart.

## Non-goals

- This is not an AI agent runtime.
- This does not own product-specific task contracts.
- This does not replace Codex, Claude, Copilot or project-local review logic.
- This does not use provider API keys for orchestration.

## File layout inside an adopted project

By default the orchestrator writes under `.orchestrator/` in the target project:

```text
.orchestrator/
  events/
    <event_id>.json
  inbox/
    signals/
      <event_id>.json
    notifications/
      <event_id>.json
    thread-wakeups/
      <event_id>.json
    logs/
      watcher-service.log
    watcher-state.json
    watcher-service.json
    watcher-heartbeat.json
```

The core package is project-neutral. A project may wrap it and choose a
different state directory, but the directory must still follow the
OrchestratorEngine contract. Product-specific legacy layouts should be adapted
by the product, not by OrchestratorEngine core.

## Quick smoke workflow

Create a terminal event and inbox signal:

```bash
orchestrator-engine --project-root /path/to/project emit \
  --task-id TASK-001 \
  --terminal-status completed \
  --result /path/to/project/result.json \
  --evidence /path/to/project/evidence.json
```

List pending signals:

```bash
orchestrator-engine --project-root /path/to/project inbox
```

Run one watcher pass:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --action record once
```

Start a current Codex thread wakeup watcher:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --action current-thread-callback \
  --target-thread-id THREAD_ID \
  service start --interval-seconds 5
```

Check health:

```bash
orchestrator-engine --project-root /path/to/project watcher service status
```

Stop:

```bash
orchestrator-engine --project-root /path/to/project watcher service stop
```

## Current-thread wakeup contract

The watcher sends a short deterministic prompt:

```text
LOCAL_AI_ORCHESTRATOR_WAKEUP v1
project: /path/to/project
event_id: ...
task_id: ...
terminal_status: completed
event: ...
evidence: ...
result: ...
requires: ORCHESTRATOR_FOLLOWUP

Read the event/evidence. Verify state and decide the next safe action.
If review is required, inspect the real diff and checks before accepting.
Do not commit or push unless the user explicitly requested it.
```

## Development

```bash
python -m unittest discover -s tests -p 'test_*.py'
ruff check .
```

Additional documentation:

- [Contracts](docs/contracts.md)
- [Project adoption](docs/project-adoption.md)
