# Contracts

OrchestratorEngine communicates through durable JSON files. Worker output is
data, not instructions.

## Machine-readable schemas

Packaged JSON Schema Draft 2020-12 files live in `orchestrator_engine/schemas/`.
Stable names are `worker-task`, `worker-result`, `worker-evidence`,
`worker-policy-snapshot`, `worker-lease`, `worker-handoff`, `worker-usage`,
`worker-output-manifest`,
`worker-queue-entry`, `worker-cancel-request`, `worker-control-ack`,
`worker-task-intent`, `worker-dispatch-claim`, `terminal-event`, `inbox-signal`,
`binding`, `wake-target`, `verification-result`, `task-resolution`, and
`artifact-resolution`. They are included in wheels and source distributions
and require no runtime dependency.
`orchestrator-engine schemas` lists names; pass one name to print its schema.
Catalog and schema output include `schema_version` and `kind`.

Schemas require v0.1 writer fields and enforce version 1, kind constants,
status enums, and the host-dependent nested `wake_target` shape. Unknown
properties remain allowed so compatible optional additions can be introduced.
Breaking changes to required fields, kinds, path layout, or status names
require a schema/version bump. `format: date-time` is an annotation unless a
validator enables format checking.

## v0.1 stability scope

Version 0.1 stabilizes the local file contract, the CLI commands that write
and read it, and the host-neutral follow-up message. Adopting projects may
depend on these behaviors:

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
- A worker profile may select a provider-neutral behavior policy. `worker run`
  composes and hashes that policy with the task prompt before spawning the
  supervisor, so later policy edits cannot change an already-dispatched task.
- `watcher --host HOST` scopes delivery to one host and uses host-specific
  callback state/service/heartbeat files by default.
- `cleanup` never removes terminal events or inbox signals.
- Task outcome resolutions are separate operator files under
  `.orchestrator/task-resolutions/`; they do not rewrite worker
  `task.json`, `result.json`, `evidence.json`, events or signals.
- Malformed-schema acknowledgements are hash-bound companion files under
  `.orchestrator/artifact-resolutions/`; they never rewrite the historical
  artifact and stop applying if its bytes change.

The following are intentionally not v0.1 core contracts:

- Product-specific task formats, policy contents, review rules, model choices
  or effort tiers. Core only validates and composes explicitly selected policy
  files; adopting projects own their behavioral text.
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

### Host delivery capabilities

`host-capabilities` emits a bounded, versioned provider-neutral report with
`schema_version`, `kind`, `host_count` and a stable `hosts` array. Every host
item has `host`, `delivery_mode` and `live_refresh_support`. Current values
are `session_stream` / `supported` for Claude, `ui_injection` /
`best_effort` for VS Code, and `headless_app_server_turn` / `unsupported` for
Codex Desktop. The latter means a `woken` Codex receipt confirms that a
headless App Server turn completed; it never asserts that an already-open
Desktop chat refreshed or received a live wakeup. Receipt fields use the same
precise enums. `ui_injection` is a stable v0.1 protocol identifier for invoking
the documented VS Code CLI, not a claim that host security is bypassed. The
`woken` status is likewise retained for v0.1 compatibility and means completed
headless history delivery when `host` is `codex`.

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
- `schema_compatibility` — durable JSON documents use supported schemas;
  hash-bound operator-resolved malformed metadata is reported separately and
  does not keep aggregate health at warning.
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

Declares which host target the `callback` watcher action should use. Written
by `orchestrator-engine bind`.

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

`wake_target` captures the host target that dispatched a specific task. This
is what makes multi-chat orchestration deterministic: if chat A starts task A
and chat B later rebinds the same project before task A finishes, task A's
signal is still routed to chat A's host target.

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

Each delivery channel only consumes signals for hosts it can handle:

- `watcher --action callback` can submit Codex history turns and handles VS
  Code chat CLI delivery. Among callback adapters, only VS Code attempts live
  UI delivery, and that support is `best_effort`.
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
policy = "quality-efficient"  # optional [policies.*] behavior bundle
availability_probe = ["/path/to/project-check", "--worker", "claude"] # optional local command
availability_timeout_seconds = 5 # required with availability_probe; 0 < value <= 300
```

Optional dispatch admission defaults:

```toml
[dispatch]
availability_mode = "off"       # off | block-unavailable | require-available
intent_enforcement = "off"      # off | permissions | strict
```

`availability_probe` is an adopting project's or adapter's non-AI local
command. Core executes it only for `worker availability` or when a dispatch
availability mode requests it. Exit 0 means
`available`; nonzero means `unavailable`; timeout or execution failure means
`probe_error`. Profiles without a probe report `not_configured` and retain
existing dispatch behavior. This report is bounded and versioned. Probe output
content is never returned: the report contains only its byte count and SHA-256
digest, so a local probe cannot accidentally leak private diagnostics.

`worker run --preflight-availability` remains a compatibility alias for
`--availability-mode block-unavailable`: only `unavailable` blocks dispatch,
while `not_configured` and `probe_error` remain advisory. In
`require-available`, every status other than `available` blocks before task
artifacts are created. A CLI mode overrides `[dispatch].availability_mode`;
specifying the legacy flag and the new option together is an error.

The preflight is a point-in-time check-then-run sequence and cannot guarantee
quota or availability at launch time. Successful checked dispatches snapshot
the mode, status, timestamp, exit code/error category and bounded output
size/hash metadata into task/evidence. Raw probe output is never stored. It is
not a provider quota API;
the engine does not poll an AI model or invent provider commands. Use
`worker availability --worker NAME` for one enabled profile, or
`worker availability --all` to include disabled profiles. The timeout must be
finite and no greater than 300 seconds, so a probe cannot hold the command
indefinitely.

### Task intent admission

The legacy `[dispatch].enforce_intent` boolean remains supported: `true` maps
to `intent_enforcement = "permissions"`, and `false` maps to `"off"`.
Configuring both forms is an error. Permission enforcement preserves the v0.2
behavior: `permission_profile` may not exceed task intent permissions.

Strict mode additionally requires project-owned compatibility declarations:

```toml
[workers.reviewer]
command = ["agent", "--non-interactive"]
permission_profile = "readonly"

[workers.reviewer.admission]
roles = ["review", "triage"]
max_risk = "high"
verification = ["structural", "focused", "full"]
authorizations = { commit = false, push = false, network = true }
```

For every field present in `WORKER_TASK_INTENT`, strict mode fails closed when
the corresponding declaration is absent or incompatible. Risk is ordered
`low < medium < high`; role and verification use exact membership. A profile
authorization set to true must also be true in task intent, with omitted task
authorization values treated as false. Admission metadata is an auditable
project assertion, not proof of model capability or provider sandbox behavior.
Successful matching is snapshotted in task/evidence as `intent_admission`, so
later edits to `workers.toml` do not change the recorded dispatch decision.

### Blocking worker wait

`worker wait --task-id TASK-ID` is the human-facing fallback for hosts that
cannot refresh an already-open chat. It reads only the task descriptor and
terminal result, refreshes one compact TTY line, and exits when the task is
terminal. Repeat `--task-id` to wait for a bounded set of up to 64 unique
tasks. `--mode all` (the default) returns when every task is terminal;
`--mode any` returns when at least one task is terminal. An unhealthy task
ends either mode with `action_required` instead of being hidden by another
task's success.

Color and the terminal bell default to `auto`; `--color` and `--bell` accept
`auto`, `always` or `never`. An optional positive `--timeout-seconds` bounds
the complete local wait, not each task separately. Exit status is `0` when the
selected condition is met and all terminal tasks in the snapshot completed,
`2` when the condition is met with an unsuccessful terminal task, `3` when
operator action is required and `124` when only the local wait times out. The
default stale threshold is three worker heartbeat intervals (90 seconds) and
can be changed with positive `--stale-after-seconds`.

`--json` suppresses the live display and emits one bounded wait-status object.
With exactly one `--task-id`, the existing `WORKER_WAIT_STATUS` contract
contains task/worker/status, bounded heartbeat metadata and terminal artifact
paths, but never worker stdout or stderr. Multiple task ids produce
`WORKER_WAIT_GROUP_STATUS` with `mode`, aggregate counts, ordered task ids,
terminal/action-required task ids, and the same bounded per-task snapshots
under `tasks`. It never embeds worker logs. `active_count` includes only
non-terminal tasks that do not currently require operator action.

The command performs deterministic filesystem reads and sleeps; it does not
invoke an AI model or wait for tasks sequentially. Agents ending a Codex
orchestration turn should show this command to the user instead of repeatedly
checking task state themselves. A dead supervisor, stale heartbeat, unreadable
lease or terminal descriptor without a readable result produces bounded
`health` metadata and `wait_status: "action_required"`; the wait never reaps,
kills or rewrites the task itself.

```bash
orchestrator-engine --project-root /path/to/project worker wait \
  --task-id TASK-A --task-id TASK-B --mode all --json
```

Host runtimes may wrap this deterministic command in one blocking tool call.
Codex may optionally use a low-cost relay subagent when its native child wait
offers a materially better blocking window, but relay behavior is a host
adapter concern rather than part of this core contract. The parent must remain
active for automatic continuation; this does not provide detached live wakeup.
See [Codex in-turn continuation](codex-in-turn-continuation.md).

**Model and effort selection happens inside `command`.** The engine does not
interpret keys like `model` or `effort` — free-form keys are recorded in each
task's `evidence.json` as audit metadata only. To control which AI runs and
how hard it thinks, pass the CLI's own flags:

```toml
[workers.claude-fast]                      # cheap: trivial checks, small edits
enabled = true
command = ["claude", "-p", "--model", "haiku", "--effort", "low",
           "--permission-mode", "dontAsk"]
prompt_via = "stdin"

[workers.claude-deep]                      # expensive: reviews, refactors
enabled = true
command = ["claude", "-p", "--model", "opus", "--effort", "xhigh",
           "--dangerously-skip-permissions"]
prompt_via = "stdin"
expect_long_running = true

[workers.codex-deep]
enabled = true
command = ["codex", "exec", "--json", "-m", "gpt-5.6-sol",
           "-c", "model_reasoning_effort=\"xhigh\"",
           "-c", "approval_policy=\"never\"",
           "-c", "sandbox_mode=\"danger-full-access\""]
prompt_via = "arg"
expect_long_running = true

[workers.copilot]
enabled = true
command = ["copilot", "--model", "auto", "--effort", "high",
           "--allow-all", "--no-ask-user", "--prompt"]
prompt_via = "arg"
expect_long_running = true
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
- `worker_policy_not_configured` — profile has no explicit composed behavior
  policy; this is `info` and preserves backward-compatible dispatch.
- `worker_policy_unreadable` — a selected policy file is missing, empty,
  non-UTF-8 or exceeds the bounded policy limits; this is an `error` for new
  dispatch while already-snapshotted tasks remain runnable.
- `copilot_may_request_approval` — Copilot profile lacks
  `--allow-all --no-ask-user`.
- `codex_may_request_approval` — `codex exec` profile lacks an explicit
  `approval_policy="never"` override or the official full-bypass flag.
- `codex_missing_sandbox_strategy` — `codex exec` profile lacks an explicit
  `--sandbox` / `sandbox_mode` setting or the official full-bypass flag; verify
  the selected policy is intentional.
- `claude_missing_permission_mode` — `claude -p` profile lacks an explicit
  `--permission-mode` or `--dangerously-skip-permissions` flag.

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

## Worker output economy

Detached workers should return compact, evidence-oriented summaries. Full
stdout, stderr, command logs, private documents and generated context payloads
belong in durable artifacts, not in host-chat messages or GitHub Issues.

Worker prompts should require:

- findings or results first;
- exact commands and pass/fail status;
- artifact paths for stdout, stderr, summaries and failed-command logs;
- small excerpts only when needed to identify a failure;
- no full log dumps for passing checks;
- no private document bodies or private planning content.

Host chats should read artifacts in this order:

1. `result.json`, `evidence.json` or `verification-result.json`;
2. `summary.txt` for verification workers;
3. specific failed-command logs or log tails only when the compact artifacts
   show a failure or the user asks for drill-down.

Check selection follows the portable
[risk-based verification policy](verification-policy.md). Documentation-only
and metadata-only work should not dispatch a test suite unless it changes a
generated artifact, packaging input or test expectation. Isolated behavior
uses focused checks; shared contracts, cross-module behavior, packaging and
release candidates use the full gate. A passing full gate remains valid after
a later prose-only edit that does not affect its scope.

`worker tasks` records log sizes for `worker-stdout.log`,
`worker-stderr.log` and `supervisor.log`. When any of these exceeds the
configured `--large-log-bytes` threshold, the task receives
`task_large_worker_log` at `info` severity, and aggregate `status` exposes
large-log counts separately. This diagnostic is a token-budget advisory: it
does not mean the task failed, only that operators should avoid pasting full
logs into chat or reports.

`checks` applies the same policy to verification `full.log` and per-command
logs. Oversized verification logs receive `verification_large_log` at `info`
severity and are surfaced through aggregate status/report visibility fields.

## Verification result

Long-running checks should run as detached workers instead of keeping a host
chat open while output streams. A project may use any native runner as long as
it writes a compact machine-readable result and durable logs. The bundled
`examples/check_runner.py` is a portable reference implementation, not core
runtime logic.

The intended control flow is dispatch/end-turn/follow-up: the host chat starts
a verification worker with `worker run`, then ends the current turn without
polling. The worker terminal event carries the task's `wake_target`, so the
result is routed through the channel selected by the dispatching host. Claude
supports live stream wakeup, VS Code attempts best-effort UI delivery, and
Codex Desktop requires durable history and manual review. This keeps long test
suites from spending host-chat tokens while they are only waiting for local
processes.

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

When a verification follow-up is received, the host agent should read
`verification-result.json` and `summary.txt` first. If `status` is `passed`,
do not read the full log unless the user asks. If `status` is not `passed`,
read the relevant command log(s) referenced by failed command entries.

The check runner itself should not invoke an AI model merely to execute or
wait for deterministic commands. A project may optionally dispatch a low-cost
analysis worker after a failed check to summarize only the referenced failure
logs. That triage is a separate task with separate evidence; it must not
replace the original verification result or the host agent's review.

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
- `verification_large_log` — one or more verification log artifacts exceed
  the configured `--large-log-bytes` threshold; emitted at `info` severity so
  large logs stay visible without making successful checks fail status checks.
- `verification_commands_invalid` — result `commands` is not a list.
- `verification_missing_result`, `verification_missing_summary`,
  `verification_missing_full_log` — referenced artifacts are missing.

Exit codes match other diagnostic commands: `0` for no diagnostics or `info`
only, `2` for warnings, `3` for errors and `1` for CLI/runtime failures such
as an unknown `--check-id` filter.

## Worker tasks

### Worker behavior policies

`workers.toml` may define reusable policy bundles and assign one explicitly to
each AI worker profile:

```toml
[policies.quality-efficient]
files = ["policies/quality-efficient.md"]
quality_priority = "correctness-first"
context_strategy = "progressive"
verification_strategy = "risk-based-final-gate"
output_strategy = "compact-evidence"

[workers.codex]
command = ["codex", "exec", "--json"]
prompt_via = "arg"
policy = "quality-efficient"
```

Policy paths are relative to the directory containing `workers.toml`; absolute
paths and `..` escapes are rejected. Each file is limited to 32 KiB and the
combined policy to 64 KiB, with at most eight files and 8 KiB of
JSON-compatible metadata. This prevents an accidental documentation dump or
oversized control table from becoming permanent prompt/evidence overhead.
Policy bytes are read once at dispatch.

When a policy is selected, `worker run` writes
`.orchestrator/tasks/<task_id>/effective-prompt.md` with explicit policy and
task boundaries. Profiles without a policy receive the same immutable task
snapshot without a policy section. `task.json` and `evidence.json` preserve:

- original `prompt_file` and dispatch-time `prompt_sha256`;
- `effective_prompt_file` and `effective_prompt_sha256`;
- `worker_policy` with policy name, metadata, per-file path/size/SHA-256,
  total bytes and `captured_at`.

The supervisor verifies the effective prompt hash and executes that immutable
snapshot without depending on the continued existence of the source prompt.
Editing or removing the task prompt, editing a policy file, or changing the
active project binding after dispatch cannot alter the policy/task bytes
already assigned to that worker.
Profiles without `policy` remain backward compatible; `worker diagnose`
reports `worker_policy_not_configured` at `info` severity so users can migrate
intentionally without breaking existing dispatch.

For the bundled `quality-efficient` policy, `worker list` and
`worker diagnose` expose the bundled revision/SHA-256 and the selected local
file SHA-256. An exact copy reports `current`; a different local file reports
`different` plus one informational `policy_update_available` diagnostic per
policy, regardless of how many profiles select it. Difference is not treated
as an error because it may be an intentional project customization. The engine
never overwrites a local policy; operators compare and update it explicitly.

The bundled quality-efficient policy is correctness-first. It saves context by
using progressive file discovery, focused checks during implementation, one
full gate on a finished high-risk candidate, bounded output and compact
handoffs. It explicitly requires escalation for security, durable data, shared
contracts, migrations, concurrency, packaging and ambiguous failures; it does
not impose a task token cap or permit skipping necessary evidence.

`effective-prompt.md` is a durable audit artifact and contains the complete
task text plus selected policy contents. It may therefore be private even when
the original prompt lived in a temporary file. Adopting projects must keep
runtime task directories out of public Git and apply an explicit project-local
retention/backup policy; core does not delete these artifacts implicitly.

`worker run` creates `.orchestrator/tasks/<task_id>/` containing:

- `task.json` — descriptor (worker, status, supervisor pid, timestamps).
- `effective-prompt.md` — dispatch-time task snapshot, with the selected policy
  prepended when configured.
- `worker-stdout.log`, `worker-stderr.log` — captured worker output.
- `result.json` — exit code, duration, failure reason, output paths.
- `evidence.json` — command, original/effective prompt hashes, policy manifest
  and worker config snapshot.
- `supervisor.log` — supervisor process output.

`task.json` has a single writer at a time. `worker run` writes the descriptor
before it spawns the supervisor, reports `status: "starting"` and then never
writes it again. The supervisor takes ownership as its first action, recording
`status: "running"` and its own `supervisor_pid`; a task that stays `starting`
was therefore dispatched but never claimed. Once the worker is spawned the
supervisor also records `worker_pid` and `worker_pgid` (the process group the
worker leads), which is the identity needed to stop the whole worker tree.

On worker exit the supervisor calls the standard terminal event contract:
`completed` on exit code 0, `failed` otherwise, `timed_out` when
`timeout_seconds` is exceeded.

A timed-out worker is stopped through its process group, not as a single
process, so the model CLI's own subprocesses cannot outlive the task: the
supervisor sends `SIGTERM` to the group, allows a bounded grace period, then
escalates to `SIGKILL`. The signal ledger is durable in `result.json` as an
optional `termination` object (`reason`, `scope`, `process_group`,
`grace_seconds`, `escalated`, `exited`, `signals`).

Workers without `timeout_seconds` may run indefinitely (hours-long tasks are
expected). While a worker runs, the supervisor refreshes `task.json` every 30
seconds with `status: "running"`, `worker_pid` and `last_alive_at`, so long
tasks remain observable. The same heartbeat renews `lease.json`, which records
Linux `/proc` process identity tokens for the supervisor and worker. These
tokens include the boot id and kernel start time; signaling code refuses to act
on a reused PID.

`worker reap` is a conservative, idempotent recovery operation. It only
finalizes a running task after its lease has expired and its recorded
supervisor identity is proven gone. Legacy tasks without leases, live but stale
supervisors, and leases without a verifiable identity are reported without
mutation. A recovered task receives a real `failed` result with
`failure_class: "supervisor_lost"`, evidence, one deterministic terminal event,
one inbox signal, and a released lease. Repeating the command does not emit a
second terminal event or delete any artifact.

### Admission, cancellation and retry

`[dispatch].max_concurrent` and `[workers.<name>].max_concurrent` are optional
positive integers. With no limits, dispatch remains immediate and backward
compatible. When a limit is full, `worker run` stores `status: "queued"` and a
FIFO entry under `.orchestrator/queue/pending/`; no provider process starts.
Admission is serialized with a POSIX `flock`. `worker queue tick` admits as many
entries as current global and profile slots permit. A finishing supervisor also
runs the same idempotent tick, so normal queue progress needs no polling daemon.

`worker cancel --task-id ID --mode graceful|forced --reason TEXT` is durable.
Queued cancellation atomically moves the queue entry to `queue/cancelled` and
emits a normal `cancelled` result/evidence/event/signal without starting the
worker. Running cancellation writes a separate control request; the supervisor
acknowledges it, signals the verified worker process group, and preserves the
request, acknowledgement and signal ledger. Repeating cancellation is safe.

Each accepted dispatch has an exact SHA-256 fingerprint over the original task
prompt, selected policy identity, task intent, worker and command. An identical
active dispatch is rejected. `--allow-duplicate --duplicate-reason TEXT` is an
explicit, audited override; no semantic or fuzzy similarity is attempted.

`worker run --intent-file intent.json` accepts the provider-neutral fields
`role`, `risk`, `verification`, `permissions` and boolean `authorizations` for
`commit`, `push` and `network`. Intent is validated, snapshotted and rendered
into the immutable effective prompt. Legacy `[dispatch].enforce_intent = true`
or `intent_enforcement = "permissions"` rejects a profile whose
`permission_profile` exceeds the intent's maximum permission level. Strict
enforcement also applies the profile admission declaration described above.
`worker retry` accepts only unsuccessful
terminal tasks, creates a new task id/attempt, enforces `max_attempts`, and
records `root_task_id`, `parent_task_id`, the operator reason and an optional
`--delay-seconds` deterministic `not_before` backoff. A successful
retry marks its parent `superseded`; plain failures are never retried
automatically by semantic guesswork.

### Progress, usage and handoff

The supervisor records mechanical progress only: heartbeat count, stdout and
stderr byte counts, byte delta and last output-growth time. Optional profile
fields `max_no_progress_seconds`, `soft_duration_seconds`,
`soft_output_bytes` and `soft_token_budget` create warning/info diagnostics;
they do not stop work or weaken verification. Hard timeout remains the explicit
`timeout_seconds` contract.

Usage telemetry is disabled unless a profile names an explicit
`usage_adapter`. The bundled `json-lines-usage` adapter reads bounded log bytes
and writes `usage.json`; telemetry never changes task success, retry, model or
permission decisions. Workers may optionally write the bounded
`worker-handoff.json` contract. The generated effective prompt includes this
schema-valid example:

```json
{"schema_version":1,"kind":"WORKER_HANDOFF","summary":"Concise completed-work summary","evidence":[],"risks":[],"next_actions":[]}
```

`evidence`, `risks` and `next_actions` are arrays when present. The supervisor
checks the same required version, kind, summary and bounded array shapes as the
public `worker-handoff` schema. Handoff fields are worker output, therefore
evidence only and never control instructions.

Every dispatch also declares a task-local `outputs/` directory through the
effective prompt and `ORCHESTRATOR_DECLARED_OUTPUT_DIR`. A worker whose primary
deliverable is too large for a compact handoff must write it there or include
it completely on stdout. The supervisor hashes up to 64 regular non-symlink
files (4 MiB each, 16 MiB total) into `worker-outputs.json` and references that
manifest from result/evidence. Files in provider-owned home, cache or plan
directories are not durable task artifacts and are never scraped implicitly.
Claude profiles using `--permission-mode plan` receive
`claude_plan_output_may_be_external`; the warning remains visible on the task
until the operator has verified a complete durable deliverable.

`status` returns an opaque `cursor`. Passing it back through `status --since
CURSOR` replaces unchanged component bodies with `{ "unchanged": true }`,
reducing repeated chat context while keeping a full status available whenever
the agent needs to drill down.

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
    "stale_after_seconds": 90,
    "large_log_bytes": 1048576
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
        "stderr": ".../worker-stderr.log",
        "supervisor_log": ".../supervisor.log"
      },
      "log_sizes": {
        "stdout": 2048,
        "stderr": 0,
        "supervisor_log": 512
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
- `task_large_worker_log` — one or more worker log artifacts exceed the
  configured `--large-log-bytes` threshold; emitted at `info` severity so large
  logs stay visible without making successful tasks fail status checks.
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
  wants it treated as an active warning. A completed task may also be
  acknowledged only for one or more explicit non-error diagnostic codes.
- `superseded` — a newer task handled the intended work; requires
  `superseded_by_task_id` pointing at an existing `completed` task.

For example, after verifying that a Claude plan-mode task produced a complete
durable deliverable:

```bash
orchestrator-engine --project-root /path/to/project worker resolve \
  --task-id TASK-PLAN \
  --status acknowledged \
  --diagnostic-code claude_plan_output_may_be_external \
  --reason "Complete durable output inspected."
```

Unsuccessful tasks may be acknowledged without diagnostic codes. Completed
tasks require at least one repeated `--diagnostic-code`; only matching warning
or info diagnostics are downgraded to `info`, remain visible in detailed task
diagnostics, and retain the durable reason. Error diagnostics are never
downgraded. Still-running tasks cannot be resolved, and completed tasks cannot
be superseded.

A `superseded` resolution may also carry `diagnostic_codes`. This preserves the
top-level `superseded_by_task_id` relationship while removing stale historical
profile warnings from normal aggregate health. To add a code to an existing
resolution, repeat its status and target with `--replace`:

```bash
orchestrator-engine --project-root /path/to/project worker resolve \
  --task-id TASK-OLD --status superseded \
  --superseded-by-task-id TASK-NEW \
  --diagnostic-code copilot_may_request_approval \
  --reason "Successful rerun used corrected worker settings." --replace
```

`worker tasks --severity info` still shows resolved unsuccessful tasks with
`task_terminal_unsuccessful_resolved`. Missing or unreadable artifacts remain
`error` diagnostics even when a task has a resolution file.

## Artifact resolutions

Path:

- `.orchestrator/artifact-resolutions/<path-and-content-identity>.json`

An artifact resolution acknowledges one durable JSON artifact currently
reported by `doctor` with malformed schema metadata. It records the path
relative to the state root, exact SHA-256, observed metadata, diagnostic code,
reason and timestamp. The original bytes are never changed or deleted.

```bash
orchestrator-engine --project-root /path/to/project artifact resolve \
  --path .orchestrator/tasks/TASK-OLD/worker-handoff.json \
  --reason "Known historical handoff prompt defect reviewed after upgrade."

orchestrator-engine --project-root /path/to/project artifact resolutions
```

The command rejects paths outside the state root, symlinks, unreadable JSON,
supported artifacts and integer unsupported schema versions. If artifact bytes
change, the old resolution becomes inactive and the warning returns. Resolving
the changed bytes creates another immutable companion record; it never
overwrites the earlier resolution. Repeating the command with the same path,
bytes and reason is idempotent; a different reason for that immutable identity
is rejected. State-relative paths printed by `artifact resolutions` can be
passed directly back to `artifact resolve`. `doctor` exposes acknowledged
findings as `resolved_malformed` while keeping unreadable and incompatible
artifacts at their original severity. Invalid companion records are reported
separately and also keep schema health at warning; stale but valid records
remain ordinary audit history.

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
- `thread-wakeups/<event_id>.json` — legacy-named host delivery receipt path,
  retained as a v0.1 file contract.

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

To acknowledge one pending or deferred event without deleting the durable audit
trail, select its host explicitly and provide the manual-review reason. The
host-scoped watcher state is updated and a versioned acknowledgement receipt is
written under `inbox/acknowledgements/<host>/`:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host HOST acknowledge --event-id EVENT_ID --reason "read manually"
```

For an intentional bulk acknowledgement of every *currently pending* signal
for one host, use the separate mode and explicit confirmation:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host HOST acknowledge --all-pending --confirm-all-pending \
  --reason "reviewed all pending signals manually"
```

Acknowledgements are idempotent: repeating an event acknowledgement returns
the original receipt rather than changing its recorded reason or timestamp.
They never remove events, signals, results or evidence.

To list deferred events:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host HOST deferred list
```

To re-arm a deferred event for the next scan without marking it handled:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host HOST deferred retry --event-id EVENT_ID --reason "quota reset"
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

Codex headless turns are guarded before submission. If `thread/read` reports
the target thread as active, or if the target thread's rollout file was
modified within the recent-activity grace window (30 seconds by default), the
receipt is written as `deferred` (`reason: "thread_active"` or
`"thread_recently_active"`) and the event remains retryable. This prevents a
worker that finishes while the orchestrating turn is still running from
creating a parallel headless turn for the same thread. The tradeoff is a short
delivery delay when a worker finishes immediately after the user's turn writes
to the rollout. Once a headless turn is started, it is watched for a short
failure window (2 minutes): failures inside the window are classified by the
watcher state machine. Quota/usage-limit failures require manual handling
instead of creating an unbounded retry loop. A turn still running at the end
of the window was submitted — orchestrator turns may legitimately run for hours — so
the receipt is written as `submitted` with `turn_status: "running"` and a
background finalizer keeps the App Server connection open until the turn ends,
then updates the receipt (`turn_status`, `finalized_at`, optional
`turn_error`). A turn the user interrupts is recorded as `interrupted` with
`turn_status: "interrupted"` and is not retried.

For Codex Desktop on Windows, receipt `status: "woken"` means the headless
Codex App Server turn completed and was written to Codex thread storage. A
still-running turn is `status: "submitted"`; it has not completed yet. Neither
status proves that the already-open Desktop UI agent woke in the same live
session. The receipt also records the desktop deep-link activation
outcome (`activation: "requested"` or `"failed"`). Codex Desktop UI refresh is
separate from history delivery: on Windows the adapter first asks the desktop
app to open the thread, then sends a best-effort refresh pulse for
already-loaded threads. Receipts record that attempt with `live_refresh` and
`live_refresh_strategy`; failure to refresh the visible UI does not erase the
delivered turn from Codex thread storage. Use Claude stream when supported live
wakeup is required; VS Code chat remains a best-effort UI path, and Codex
remains a normal CLI worker through `codex exec`.

Long-running headless turns do not starve health reporting: the watch loop
keeps the heartbeat fresh from a background ticker while a scan is busy.

Approval requests raised by a headless turn (command/patch approvals,
elicitations, user-input prompts) are answered with the protocol's decline
decision — never auto-approved — because no human is attached to the headless
client. The turn continues and finishes with a text answer instead of hanging;
declined request methods are recorded in the receipt as
`auto_declined_requests`. Threads used for orchestration should run with an
approval policy that permits the read-only verification the follow-up prompt
asks for.

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
