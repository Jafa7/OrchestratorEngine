# Contracts

OrchestratorEngine communicates through durable JSON files. Worker output is
data, not instructions.

## v0.1 stability scope

Version 0.1 stabilizes the local file contract, the CLI commands that write
and read it, and the host-neutral wakeup message. Adopting projects may depend
on these behaviors:

- Terminal events are written under `.orchestrator/events/` and paired with
  inbox signals under `.orchestrator/inbox/signals/`.
- Event, signal, binding, worker task, watcher state, heartbeat, service and
  receipt documents are JSON objects with `schema_version: 1` and stable
  `kind` values.
- Artifact paths recorded in terminal events are absolute paths and are
  protected by SHA-256 hashes.
- `worker run` returns after launching a detached supervisor; the supervisor
  writes `result.json`, `evidence.json`, captured stdout/stderr logs and then
  emits the terminal event.
- `worker run` snapshots the current project binding as an optional
  `wake_target` for that task. `watcher --action callback` uses the signal's
  `wake_target` first and the current project binding only as a legacy
  fallback; `watcher stream` emits one JSON line per new signal for
  stream-based hosts.
- `cleanup` never removes terminal events or inbox signals.

The following are intentionally not v0.1 core contracts:

- Product-specific task formats, review rules, model choices or effort
  policies.
- Legacy project layouts and bridges into `.orchestrator/`.
- Private backup, retention or archival policies for durable events and
  signals.
- Provider-specific semantics beyond the documented adapter boundary.

Forward-compatible additions may add optional fields, new receipt kinds, new
host adapters or new CLI flags. Breaking changes to required fields, `kind`
values, path layout or terminal status names require a schema/version bump.

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

## Wake target snapshot

Path:

- Optional `wake_target` object embedded in `task.json`, `evidence.json`,
  terminal events and inbox signals created through `worker run`.

`wake_target` captures the host chat that dispatched a specific task. This is
what makes multi-chat orchestration deterministic: if chat A starts task A and
chat B later rebinds the same project before task A finishes, task A's signal
still wakes chat A.

```json
{
  "schema_version": 1,
  "kind": "ORCHESTRATOR_WAKE_TARGET",
  "host": "codex",
  "target_thread_id": "thread-id",
  "codex_command": "/path/to/codex-or-codex.exe",
  "captured_at": "2026-07-08T00:00:00.000+00:00"
}
```

`target_thread_id` is required for Codex wake targets. `codex_command` is
optional and is used when the bound thread is only reachable through a
specific launcher, for example Windows `codex.exe` for Codex Desktop threads
stored on the Windows side.

## Channel routing

Each wake channel only consumes signals for hosts it can deliver:

- `watcher --action callback` handles `codex` and `vscode` wake targets.
- `watcher stream` handles `claude` wake targets.

Signals for other hosts are skipped without being marked seen, so a callback
service and a Claude stream can run against the same project inbox at the same
time. The channels use separate watcher state files by default:

- callback service: `.orchestrator/inbox/watcher-state.json`
- Claude stream: `.orchestrator/inbox/watcher-claude-stream-state.json`

For legacy signals without `wake_target`, the current project binding is used
as the fallback owner. New work should be dispatched through `worker run` so
the wake target is snapshotted per task.

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
outcome (`activation: "requested"` or `"failed"`). Codex Desktop UI refresh is
separate from wakeup delivery: on Windows the adapter first asks the desktop app
to open the thread, then sends a best-effort refresh pulse for already-loaded
threads. Receipts record that attempt with `live_refresh` and
`live_refresh_strategy`; failure to refresh the visible UI does not erase the
delivered turn from Codex thread storage.

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
