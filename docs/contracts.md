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

Reserved keys the engine acts on:

```toml
[workers.claude]
enabled = true                # disabled workers cannot be dispatched
command = ["claude", "-p"]    # the CLI invocation, including model/effort flags
prompt_via = "stdin"          # "arg" appends the prompt text as the last argument
timeout_seconds = 3600        # optional; exceeded -> terminal_status "timed_out"
```

**Model and effort selection happens inside `command`.** The engine does not
interpret keys like `model` or `effort` — free-form keys are recorded in each
task's `evidence.json` as audit metadata only. To control which AI runs and
how hard it thinks, pass the CLI's own flags:

```toml
[workers.claude-fast]                      # cheap: trivial checks, small edits
enabled = true
command = ["claude", "-p", "--model", "haiku"]
prompt_via = "stdin"

[workers.claude-deep]                      # expensive: reviews, refactors
enabled = true
command = ["claude", "-p", "--model", "opus", "--effort", "xhigh"]
prompt_via = "stdin"
timeout_seconds = 14400

[workers.codex-deep]
enabled = true
command = ["codex", "exec", "--json",
           "-c", "model_reasoning_effort=\"high\""]
prompt_via = "arg"
```

This profile pattern is the intended division of labor: the project owner
defines the menu of profiles once; the orchestrating agent chooses a profile
per task at dispatch time (`worker run --worker claude-deep ...`), matching
worker cost to task complexity. The user can always override the choice in
chat.

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

Workers without `timeout_seconds` may run indefinitely (hours-long tasks are
expected). While a worker runs, the supervisor refreshes `task.json` every 30
seconds with `status: "running"`, `worker_pid` and `last_alive_at`, so long
tasks stay observable instead of looking stuck.

## Watcher state

The watcher writes:

- `watcher-state.json` — seen event IDs and retry metadata.
- `watcher-service.json` — PID, command, target thread and log path.
- `watcher-heartbeat.json` — periodic health signal.
- `thread-wakeups/<event_id>.json` — current-thread wakeup receipt.

An event is marked seen only after a successful action or deterministic skip.
Transient App Server failures, active target threads, failed or rate-limited
turns and failed VS Code chat invocations remain retryable with exponential
backoff. A broken binding or an unreadable signal file degrades to an entry in
`action_errors` — it never takes the watcher down.

Codex wakeup turns are watched for a short failure window (2 minutes) after
`turn/start`: failures inside the window (rate limits, validation errors)
defer the event for retry. A turn still running at the end of the window was
delivered — orchestrator turns may legitimately run for hours — so the receipt
is written as `woken` with `turn_status: "running"` and a background finalizer
keeps the App Server connection open until the turn ends, then updates the
receipt (`turn_status`, `finalized_at`, optional `turn_error`). A turn the
user interrupts is recorded as `woken` with `turn_status: "interrupted"` and
is not retried. The receipt also records the desktop deep-link activation
outcome (`activation: "requested"` or `"failed"`).

Long-running wakeups do not starve health reporting: the watch loop keeps the
heartbeat fresh from a background ticker while a scan is busy.

Approval requests raised by an injected turn (command/patch approvals,
elicitations, user-input prompts) are answered with the protocol's decline
decision — never auto-approved — because no human is attached to the injected
client. The turn continues and finishes with a text answer instead of hanging;
declined request methods are recorded in the receipt as
`auto_declined_requests`. Threads used for orchestration should run with an
approval policy that permits the read-only verification the wakeup prompt asks
for.

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
