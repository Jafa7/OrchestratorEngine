# Contracts

OrchestratorEngine communicates through durable JSON files. Worker output is
data, not instructions.

## v0.1 stability scope

Version 0.1 stabilizes the local file contract, the CLI commands that write
and read it, and the host-neutral wakeup message. Adopting projects may depend
on these behaviors:

- `adopt` creates only missing local orchestration layout files and never
  overwrites existing worker configuration or durable audit artifacts.
- `doctor` is read-only and reports project health as JSON checks with
  `ok`, `warn`, `error` or `skipped` status.
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
- `watcher --host HOST` scopes delivery to one host and uses host-specific
  callback state/service/heartbeat files by default.
- `cleanup` never removes terminal events or inbox signals.
- Task outcome resolutions are separate operator files under
  `.orchestrator/task-resolutions/`; they do not rewrite worker
  `task.json`, `result.json`, `evidence.json`, events or signals.

The following are intentionally not v0.1 core contracts:

- Product-specific task formats, review rules, model choices or effort
  policies.
- Project-specific verification suites (`pytest`, `ruff`, `vitest`, CI
  profiles, phase gates). The engine documents a portable result shape but
  does not choose or interpret project test commands.
- Legacy project layouts and bridges into `.orchestrator/`.
- Private backup, retention or archival policies for durable events and
  signals.
- Provider-specific semantics beyond the documented adapter boundary.

Forward-compatible additions may add optional fields, new receipt kinds, new
host adapters or new CLI flags. Breaking changes to required fields, `kind`
values, path layout or terminal status names require a schema/version bump.

## Operator diagnostics

`adopt` writes missing local layout only:

```bash
orchestrator-engine --project-root /path/to/project adopt --host codex
```

It returns `ORCHESTRATOR_ADOPTION` with `created`, `skipped`, `dry_run` and
`next_steps`. The command does not write `binding.json`, enable workers,
overwrite `.orchestrator/workers.toml` or touch durable events/signals.

`doctor` performs read-only checks:

```bash
orchestrator-engine --project-root /path/to/project doctor
```

It returns `ORCHESTRATOR_DOCTOR_REPORT` with `checks[]` entries:

- `state_layout` — required local directories exist and are writable.
- `schema_compatibility` — durable JSON documents use supported schemas.
- `binding` — `binding.json` is present and valid.
- `workers` — `.orchestrator/workers.toml` is parseable and dispatchable.
- `watcher_channel` — callback service or stream state matches the host.
- `engine_import` — the installed Python environment can re-exec the engine.

`doctor` exits `0` for `ok` and `warn`, exits `2` for `error`, and exits `2`
for warnings only when `--strict` is passed. CLI/runtime errors still exit `1`.

`status` is the compact read-only operator report:

```bash
orchestrator-engine --project-root /path/to/project status
```

It returns `ORCHESTRATOR_STATUS_REPORT` with:

- `components.doctor` — compact `doctor` check statuses without large schema
  survey payloads.
- `components.worker_profiles` — configured/enabled worker counts and profile
  warnings.
- `components.wake_channel` — callback service or stream status, pending
  signal counts and channel warnings.
- `components.worker_tasks` — task status counts plus only tasks that have
  diagnostics at the selected severity, with counts for operator-resolved
  historical task outcomes.
- `components.checks` — verification check status counts plus only failed or
  diagnostic-bearing checks.
- `issues[]` — flattened operator actions collected from the component
  diagnostics.

`status` does not run workers, start watchers, retry callbacks or mutate state.
It exits `0` for a clean report, `2` when the worst component severity is
`warning`, `3` for `error` and `1` for CLI/runtime failures. Use
`--severity error` to suppress warning-level task/check diagnostics in the
aggregate report.

`report draft` is a read-only operator reporting helper:

```bash
orchestrator-engine --project-root /path/to/project \
  report draft --project-name PROJECT
```

It runs the compact status aggregation and prints Markdown suitable for a
GitHub Issue. It does not create network requests, mutate state or read large
logs. See [operator-reporting.md](operator-reporting.md).

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
- host-scoped callback service:
  `.orchestrator/inbox/watcher-<host>-callback-state.json`
- Claude stream: `.orchestrator/inbox/watcher-claude-stream-state.json`

Host-scoped callback services are recommended when multiple callback hosts
share one inbox. The legacy unscoped callback service remains compatible and
acts as one combined callback channel for `codex` and `vscode`.

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
expect_long_running = false   # optional; suppresses missing-timeout info when true
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

`worker list` and `worker run` may include advisory `warnings` for profiles
that look risky in detached, non-interactive execution. Warnings are
machine-readable diagnostics only; they do not block dispatch and they do not
rewrite commands. For example, a Copilot profile that omits autonomous flags
such as `--allow-all --no-ask-user` is likely to stall on approval prompts, so
the engine reports `copilot_may_request_approval`. Known advisory codes:

- `worker_timeout_absent` — profile has no `timeout_seconds`; this is
  `info`, not a warning. Use `expect_long_running = true` for AI
  implementation/review profiles where no timeout is intentional.
- `copilot_may_request_approval` — Copilot profile lacks
  `--allow-all --no-ask-user`.
- `codex_may_request_approval` — `codex exec` profile lacks an explicit
  `approval_policy="never"` override or equivalent non-interactive policy.
- `codex_missing_sandbox_strategy` — `codex exec` profile lacks an explicit
  `sandbox_mode` override; verify the selected config is intentional.
- `claude_missing_permission_mode` — `claude -p` profile lacks an explicit
  `--permission-mode`.

`worker diagnose` is the read-only deep registry diagnostic command. It never
dispatches workers or rewrites commands.

```bash
orchestrator-engine --project-root /path/to/project worker diagnose \
  --enabled-only --severity warning
```

It returns:

```json
{
  "kind": "WORKER_DIAGNOSTICS",
  "worker_count": 1,
  "diagnostic_count": 1,
  "severity_counts": {"info": 0, "warning": 1, "error": 0},
  "worst_severity": "warning",
  "workers": {
    "copilot": {
      "diagnostics": [
        {
          "code": "copilot_may_request_approval",
          "severity": "warning",
          "message": "...",
          "suggested_action": "..."
        }
      ]
    }
  }
}
```

Exit codes are deterministic for automation: `0` for no diagnostics or `info`
only, `2` when the worst diagnostic is `warning`, `3` when the worst diagnostic
is `error`, and `1` for CLI/runtime failures such as invalid TOML or an unknown
`--worker` filter.

## Verification result

Long-running checks should run as detached workers instead of keeping a host
chat open while output streams. A project may use any native runner as long as
it writes a compact machine-readable result and durable logs. The bundled
`examples/check_runner.py` is a portable reference implementation, not core
runtime logic.

The intended control flow is sleep/wake: the host chat dispatches a
verification worker with `worker run`, then ends the current turn without
polling. The worker terminal event carries the task's `wake_target`, so the
watcher wakes the same chat that launched the check when the result is ready.
This keeps long test suites from spending host-chat tokens while they are only
waiting for local processes.

Recommended path layout:

- `.orchestrator/checks/<check_id>/verification-result.json`
- `.orchestrator/checks/<check_id>/summary.txt`
- `.orchestrator/checks/<check_id>/full.log`
- `.orchestrator/checks/<check_id>/<command-label>.log`

Reference JSON shape:

```json
{
  "schema_version": 1,
  "kind": "ORCHESTRATOR_VERIFICATION_RESULT",
  "check_id": "CHECK-001",
  "suite": "full",
  "status": "passed",
  "exit_code": 0,
  "started_at": "2026-07-08T00:00:00.000+00:00",
  "finished_at": "2026-07-08T00:02:39.000+00:00",
  "duration_seconds": 159.0,
  "commands": [
    {
      "label": "unit",
      "required": true,
      "status": "passed",
      "exit_code": 0,
      "duration_seconds": 104.4,
      "cwd": ".",
      "argv": ["uv", "run", "python", "-m", "unittest"],
      "command": "uv run python -m unittest",
      "log_path": ".orchestrator/checks/CHECK-001/unit.log",
      "output_tail": [],
      "output_line_count": 120
    }
  ],
  "result_path": ".orchestrator/checks/CHECK-001/verification-result.json",
  "summary_path": ".orchestrator/checks/CHECK-001/summary.txt",
  "log_path": ".orchestrator/checks/CHECK-001/full.log"
}
```

Allowed top-level `status` values:

- `passed` — all required commands passed.
- `failed` — at least one required command failed or timed out.
- `errored` — the runner could not start or execute a required command.
- `cancelled` — reserved for project runners that support cancellation.

Command statuses may additionally use `timed_out`. Paths inside the project
should be relative to the project root so results stay portable. Full stdout
and stderr belong in log files; `verification-result.json` should keep only a
short `output_tail` suitable for failure triage.

When a host chat wakes for a verification worker, it should read
`verification-result.json` and `summary.txt` first. If `status` is `passed`,
do not read the full log unless the user asks. If `status` is not `passed`,
read the relevant command log(s) referenced by failed command entries.

`checks` is the read-only status command for verification artifacts. It does
not run commands or mutate check directories.

```bash
orchestrator-engine --project-root /path/to/project checks --severity warning
```

It returns:

```json
{
  "kind": "ORCHESTRATOR_CHECKS_STATUS",
  "check_count": 1,
  "status_counts": {"passed": 0, "failed": 1, "errored": 0},
  "diagnostic_count": 1,
  "severity_counts": {"info": 0, "warning": 1, "error": 0},
  "worst_severity": "warning",
  "checks": {
    "CHECK-001": {
      "check_id": "CHECK-001",
      "status": "failed",
      "summary_path": ".orchestrator/checks/CHECK-001/summary.txt",
      "failed_command_count": 1,
      "failed_commands": [
        {
          "label": "unit",
          "status": "failed",
          "log_path": ".orchestrator/checks/CHECK-001/unit.log"
        }
      ],
      "diagnostics": [
        {
          "code": "verification_unsuccessful",
          "severity": "warning",
          "message": "...",
          "suggested_action": "..."
        }
      ]
    }
  }
}
```

Known check diagnostic codes:

- `verification_result_unreadable` — `verification-result.json` is missing or
  cannot be read as a JSON object.
- `verification_check_id_mismatch` — result `check_id` does not match its
  check directory name.
- `verification_schema_unsupported` — result schema is not supported.
- `verification_kind_unexpected` — result kind is not
  `ORCHESTRATOR_VERIFICATION_RESULT`.
- `verification_status_unknown` — result status is outside the known check
  states.
- `verification_unsuccessful` — result status is `failed`, `errored` or
  `cancelled`.
- `verification_commands_invalid` — result `commands` is not a list.
- `verification_missing_result`, `verification_missing_summary`,
  `verification_missing_full_log` — referenced artifacts are missing.

Exit codes match other diagnostic commands: `0` for no diagnostics or `info`
only, `2` for warnings, `3` for errors and `1` for CLI/runtime failures such
as an unknown `--check-id` filter.

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

`worker tasks` is the read-only runtime diagnostic command for these artifacts.
It does not execute workers, retry tasks or mutate state.

```bash
orchestrator-engine --project-root /path/to/project worker tasks \
  --worker copilot --severity warning --stale-after-seconds 90
```

It returns:

```json
{
  "kind": "WORKER_TASK_DIAGNOSTICS",
  "generated_at": "2026-07-09T00:00:00.000+00:00",
  "filters": {
    "task_id": null,
    "worker": "copilot",
    "status": null,
    "minimum_severity": "warning",
    "stale_after_seconds": 90
  },
  "task_count": 1,
  "status_counts": {"running": 1},
  "resolution_counts": {"acknowledged": 0, "superseded": 0},
  "diagnostic_count": 1,
  "severity_counts": {"info": 0, "warning": 1, "error": 0},
  "worst_severity": "warning",
  "tasks": {
    "TASK-001": {
      "status": "running",
      "worker": "copilot",
      "heartbeat_age_seconds": 120.4,
      "supervisor_pid": 1234,
      "supervisor_alive": true,
      "worker_pid": 1235,
      "worker_alive": true,
      "artifacts": {
        "result": ".../result.json",
        "evidence": ".../evidence.json",
        "stdout": ".../worker-stdout.log",
        "stderr": ".../worker-stderr.log"
      },
      "diagnostics": [
        {
          "code": "task_heartbeat_stale",
          "severity": "warning",
          "message": "...",
          "suggested_action": "..."
        }
      ]
    }
  }
}
```

Known task diagnostic codes:

- `task_descriptor_unreadable` — `task.json` cannot be read as an object.
- `task_id_mismatch` — descriptor `task_id` does not match its task
  directory name.
- `task_schema_unsupported` — descriptor schema is not supported.
- `task_kind_unexpected` — descriptor kind is not `WORKER_TASK`.
- `task_status_unknown` — descriptor status is outside the known task states.
- `task_running_without_supervisor_pid` — running task has no supervisor pid.
- `task_supervisor_dead` — running task's supervisor pid is not alive.
- `task_worker_dead` — running task's worker pid is not alive.
- `task_running_without_heartbeat` — running task lacks usable timestamps.
- `task_heartbeat_stale` — running task heartbeat age exceeds the configured
  stale threshold.
- `task_terminal_unsuccessful` — task ended in a non-`completed` terminal
  status such as `failed` or `timed_out`.
- `task_terminal_unsuccessful_resolved` — the unsuccessful terminal status is
  still recorded, but an operator resolution file exists; emitted at `info`
  severity so normal warning-level status reports do not reopen handled
  historical failures.
- `task_resolution_unreadable` — the operator resolution file is invalid or
  unreadable.
- `task_missing_result`, `task_missing_evidence`, `task_missing_event`,
  `task_missing_signal` — terminal task references missing artifacts.
- `task_unreadable_result`, `task_unreadable_evidence` — terminal artifacts
  exist but cannot be read as JSON objects.

Exit codes match `worker diagnose`: `0` for no diagnostics or `info` only, `2`
for warnings, `3` for errors and `1` for CLI/runtime failures such as an
unknown `--task-id` filter.

## Worker task resolutions

Path:

- `.orchestrator/task-resolutions/<task_id>.json`

Task resolutions are explicit operator decisions for historical task outcomes.
They preserve the durable audit trail: the original task descriptor, terminal
result/evidence, event and signal are not rewritten or removed. Use them when
a failed task has been manually reviewed, or when a newer task supersedes a
failed attempt.

Write a resolution:

```bash
orchestrator-engine --project-root /path/to/project worker resolve \
  --task-id TASK-OLD \
  --status superseded \
  --superseded-by-task-id TASK-NEW \
  --reason "Successful rerun completed the intended work."
```

List resolutions:

```bash
orchestrator-engine --project-root /path/to/project worker resolutions
```

Required fields:

```json
{
  "schema_version": 1,
  "kind": "WORKER_TASK_RESOLUTION",
  "task_id": "TASK-OLD",
  "status": "superseded",
  "superseded_by_task_id": "TASK-NEW",
  "previous_task_status": "failed",
  "reason": "Successful rerun completed the intended work.",
  "created_at": "2026-07-09T00:00:00.000+00:00"
}
```

Allowed `status` values:

- `acknowledged` — the operator inspected the unsuccessful task and no longer
  wants it treated as an active warning.
- `superseded` — a newer task handled the intended work; requires
  `superseded_by_task_id` pointing at an existing `completed` task.

The source task must already have an unsuccessful terminal status
(`failed`, `timed_out`, `rate_limited`, `invalid_result` or `cancelled`).
Completed and still-running tasks cannot be resolved.

`worker tasks --severity info` still shows resolved unsuccessful tasks with
`task_terminal_unsuccessful_resolved`. Missing or unreadable artifacts remain
`error` diagnostics even when a task has a resolution file.

## Watcher state

The watcher writes:

- `watcher-state.json` — seen event IDs and retry metadata.
- `watcher-service.json` — PID, command, target thread and log path.
- `watcher-heartbeat.json` — periodic health signal.
- `watcher-<host>-callback-state.json` — host-scoped callback seen/deferred
  state.
- `watcher-<host>-callback-service.json` — host-scoped callback service
  process state.
- `watcher-<host>-callback-heartbeat.json` — host-scoped callback heartbeat.
- `watcher-claude-stream-state.json` — Claude stream seen-event state and
  scan heartbeat.
- `thread-wakeups/<event_id>.json` — current-thread wakeup receipt.

An event is marked seen only after a successful action, deterministic skip or
manual acknowledgement. Active target threads remain retryable with
exponential backoff. Callback delivery failures are bounded: ordinary
transport failures become `deferred_manual_required` after a small number of
attempts, while recognized quota/usage-limit failures become
`deferred_manual_required` immediately. A broken binding or an unreadable
signal file degrades to an entry in `action_errors` — it never takes the
watcher down.

Deferred callback state is kept in `watcher-state.json` under
`deferred_events`:

- `deferred_retryable` — watcher will retry after `retry_after_at`.
- `deferred_manual_required` — watcher will not retry automatically; an
  operator should read the event/result/evidence and either fix the wake
  channel or acknowledge the event.
- `acknowledged` — recorded in `acknowledged_events` after manual resolution;
  the event id is also added to `seen_event_ids`.

To acknowledge a pending or deferred event without deleting the durable audit
trail:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  acknowledge --event-id EVENT_ID --reason "read manually"
```

To list deferred events:

```bash
orchestrator-engine --project-root /path/to/project watcher deferred list
```

To re-arm a deferred event for the next scan without marking it handled:

```bash
orchestrator-engine --project-root /path/to/project watcher deferred retry \
  --event-id EVENT_ID --reason "quota reset"
```

Pass the same `--host HOST` the service was started with (or an explicit
`--state-file`) so `acknowledge`, `deferred list` and `deferred retry` operate
on that service's host-scoped state file rather than the legacy
`watcher-state.json`.

For host-scoped callback services, also pass `--host HOST` to
`watcher service status`. A bare status command reads the legacy callback
files. If the project binding points at a callback host and the host-scoped
pending count differs from the legacy view, the response includes a warning
that names the exact `watcher --host HOST service status` command to run.

`watcher service status` includes `deferred_event_count` and
`deferred_status_counts`, plus `deferred_events[]` entries with event id, task
id, terminal status, attempts, last reason, evidence paths, next retry and
suggested operator action.

Codex wakeup turns are guarded before injection. If `thread/read` reports the
target thread as active, or if the target thread's rollout file was modified
within the recent-activity grace window (30 seconds by default), the receipt is
written as `deferred` (`reason: "thread_active"` or
`"thread_recently_active"`) and the event remains retryable. This prevents a
worker that finishes while the orchestrating turn is still running from
creating a parallel injected turn in the same chat. The tradeoff is a short
wakeup delay when a worker finishes immediately after the user's turn writes to
the rollout. Once a wakeup turn is started, it is watched for a short failure
window (2 minutes): failures inside the window are classified by the watcher
state machine. Quota/usage-limit failures require manual handling instead of
creating an unbounded retry loop. A turn still running at the end of the
window was delivered — orchestrator turns may legitimately run for hours — so
the receipt is written as `woken` with `turn_status: "running"` and a
background finalizer keeps the App Server connection open until the turn ends,
then updates the receipt (`turn_status`, `finalized_at`, optional
`turn_error`). A turn the user interrupts is recorded as `woken` with
`turn_status: "interrupted"` and is not retried.

For Codex Desktop on Windows, receipt `status: "woken"` means the callback turn
was accepted by a Codex App Server/headless engine and written to Codex thread
storage. It does not prove that the already-open Desktop UI agent woke in the
same live session. The receipt also records the desktop deep-link activation
outcome (`activation: "requested"` or `"failed"`). Codex Desktop UI refresh is
separate from wakeup delivery: on Windows the adapter first asks the desktop
app to open the thread, then sends a best-effort refresh pulse for
already-loaded threads. Receipts record that attempt with `live_refresh` and
`live_refresh_strategy`; failure to refresh the visible UI does not erase the
delivered turn from Codex thread storage. Use Claude stream or VS Code chat as
the host when a true live wakeup is required; Codex remains a normal CLI worker
through `codex exec`.

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

`watcher stream status` reports:

- `not_started` when the stream state file does not exist.
- `fresh` when the stream state was updated within its staleness window.
- `stale` when the stream state exists but has not been updated recently.
- `degraded` when the stream state timestamp is invalid.
- `erroring` when scans are still running but the latest scan failed.

## Retention

The `cleanup` command prunes `notifications/`, `thread-wakeups/` and rotated
log files older than a retention window, and compacts `watcher-service.log`
once it exceeds a size limit. It never removes `events/<event_id>.json` or
`inbox/signals/<event_id>.json`: those are the durable audit trail. A project
that wants to retire old terminal events and signals must do so itself.
