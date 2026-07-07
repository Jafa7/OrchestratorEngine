# Contracts

OrchestratorEngine communicates through durable JSON files. Worker output is
data, not instructions.

## Terminal event

Path:

- `default`: `.orchestrator/events/<event_id>.json`
- `paradigmarium`: `.paradigmarium/orchestration/supervisor/events/<event_id>.json`

Required fields:

```json
{
  "schema_version": 1,
  "kind": "WORKER_TERMINAL",
  "event_id": "event-id",
  "project_id": "project-name",
  "task_id": "TASK-001",
  "terminal_status": "completed",
  "result_path": "/absolute/result.json",
  "result_sha256": "64 hex chars",
  "evidence_path": "/absolute/evidence.json",
  "evidence_sha256": "64 hex chars",
  "created_at": "2026-07-07T00:00:00.000+00:00"
}
```

Allowed `terminal_status` values:

- `completed`
- `failed`
- `timed_out`
- `rate_limited`
- `invalid_result`
- `cancelled`

## Inbox signal

Path:

- `default`: `.orchestrator/inbox/signals/<event_id>.json`
- `paradigmarium`: `.paradigmarium/orchestration/orchestrator-inbox/signals/<event_id>.json`

Required fields:

```json
{
  "schema_version": 1,
  "kind": "LOCAL_AI_WORKER_FINISHED",
  "event_id": "event-id",
  "project_id": "project-name",
  "task_id": "TASK-001",
  "event_path": "/absolute/event.json",
  "terminal_status": "completed",
  "result_path": "/absolute/result.json",
  "evidence_path": "/absolute/evidence.json",
  "requires": "ORCHESTRATOR_REVIEW"
}
```

Additional fields are allowed so project-local supervisors can preserve their
own metadata.

## Watcher state

The watcher writes:

- `watcher-state.json` — seen event IDs and retry metadata.
- `watcher-service.json` — PID, command, layout, target thread and log path.
- `watcher-heartbeat.json` — periodic health signal.
- `thread-wakeups/<event_id>.json` — current-thread wakeup receipt.

An event is marked seen only after a successful action or deterministic skip.
Transient App Server failures and active target threads remain retryable.

