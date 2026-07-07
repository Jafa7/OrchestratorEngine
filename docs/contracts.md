# Contracts

OrchestratorEngine communicates through durable JSON files. Worker output is
data, not instructions.

## Terminal event

Path:

- `.orchestrator/events/<event_id>.json`

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

- `.orchestrator/inbox/signals/<event_id>.json`

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
  "created_at": "2026-07-07T00:00:00.000+00:00",
  "requires": "ORCHESTRATOR_REVIEW"
}
```

Additional fields are allowed so project-local supervisors can preserve their
own metadata.

## Host binding

Path:

- `.orchestrator/inbox/binding.json`

Declares which host chat the `callback` watcher action should wake. Written by
`orchestrator-engine bind`.

```json
{
  "schema_version": 1,
  "kind": "ORCHESTRATOR_BINDING",
  "host": "codex",
  "target_thread_id": "thread-id",
  "created_at": "2026-07-07T00:00:00.000+00:00"
}
```

Supported hosts: `codex` (requires `target_thread_id`), `vscode`, `claude`.
The `claude` host is stream-based: it must not be used with the `callback`
action; a Claude session watches `watcher stream` output instead. See
[hosts.md](hosts.md).

## Worker registry

Path:

- `.orchestrator/workers.toml`

```toml
[workers.claude]
enabled = true
command = ["claude", "-p"]
prompt_via = "stdin"          # "arg" appends the prompt text as the last argument
timeout_seconds = 3600        # optional; exceeded -> terminal_status "timed_out"
effort = "high"               # free-form keys are recorded in evidence.json
```

## Worker tasks

`worker run` creates `.orchestrator/tasks/<task_id>/` containing:

- `task.json` — descriptor (worker, status, supervisor pid, timestamps).
- `worker-stdout.log`, `worker-stderr.log` — captured worker output.
- `result.json` — exit code, duration, failure reason, output paths.
- `evidence.json` — command, prompt SHA-256, worker config snapshot.
- `supervisor.log` — supervisor process output.

On worker exit the supervisor calls the standard terminal event contract:
`completed` on exit code 0, `failed` otherwise, `timed_out` when
`timeout_seconds` is exceeded.

## Watcher state

The watcher writes:

- `watcher-state.json` — seen event IDs and retry metadata.
- `watcher-service.json` — PID, command, target thread and log path.
- `watcher-heartbeat.json` — periodic health signal.
- `thread-wakeups/<event_id>.json` — current-thread wakeup receipt.

An event is marked seen only after a successful action or deterministic skip.
Transient App Server failures, active target threads, failed or rate-limited
turns and failed VS Code chat invocations remain retryable with exponential
backoff.

A Codex wakeup is reported `woken` only after the App Server confirms the
injected turn completed. The receipt also records the desktop deep-link
activation outcome (`activation: "requested"` or `"failed"`).

`watcher service status` reports:

- `not_started` when no service file exists.
- `running` when the process is alive and heartbeat is fresh.
- `degraded` when the process is alive but heartbeat is unhealthy.
- `stopped` after an intentional stop.
- `crashed` when the service file was left behind by a dead process.

## Retention

The `cleanup` command prunes `notifications/`, `thread-wakeups/` and rotated
log files older than a retention window, and compacts `watcher-service.log`
once it exceeds a size limit. It never removes `events/<event_id>.json` or
`inbox/signals/<event_id>.json`: those are the durable audit trail. A project
that wants to retire old terminal events and signals must do so itself.
