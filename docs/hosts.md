# Host setup

OrchestratorEngine routes worker completions back to the host target the user
orchestrates from. Each host has a different delivery mechanism; the binding
contract tells the watcher which one to use. Distinguish durable delivery from
live wakeup:

- **Durable delivery** means the completion is written into the target host's
  history or inbox and the audit trail points to event/result/evidence.
- **Live wakeup** means the already-open host chat receives the message and
  the active agent continues in that same visible session.

Everything engine-side runs where the CLI workers run (typically WSL).
Windows-side actions (the Codex deep link, `code` CLI) are reached through the
normal WSL interop.

Machine-readable capabilities are available with
`orchestrator-engine host-capabilities`:

| Host | `delivery_mode` | `live_refresh_support` |
| --- | --- | --- |
| Claude | `session_stream` | `supported` |
| VS Code | `ui_injection` | `best_effort` |
| Codex Desktop | `headless_app_server_turn` | `unsupported` |

This is a versioned report with `schema_version`, `kind`, `host_count` and a
bounded, stable `hosts` collection. These describe delivery quality, not
deep-link or window activation success.

`ui_injection` is a stable machine-readable v0.1 identifier for invoking the
documented VS Code chat CLI. It does not mean that the engine bypasses host
security. All adapters use user-installed local CLIs or interfaces under the
user's account and an explicit project binding; OrchestratorEngine does not
access provider accounts directly or bypass authentication.

## Codex Desktop (Windows app, WSL mode)

Delivery mechanism: submit a turn through a headless Codex App Server process.

Live status: durable delivery only on Windows Desktop. The submitted turn is
handled by an App Server/headless engine and written to Codex thread storage.
The already-open Desktop chat does not reliably wake as the same live agent;
the new turn may become visible only after thread switch, reload, restart or
delayed UI refresh. Treat the deep link and `live_refresh` fields as
best-effort focus/refresh diagnostics, not proof that the visible Desktop
agent woke.

1. Run the bind command from the Codex chat that will dispatch work. The engine
   auto-detects that chat's thread id:

```bash
orchestrator-engine --project-root /path/to/project bind --host codex
```

Confirm `thread_id_source` and `target_thread_id` in the output. Use explicit
`--thread-id THREAD_ID` only when auto-detection fails or an operator is binding
a different chat.

Notes:

- A `status: "woken"` receipt means the headless App Server turn completed.
  It does **not** mean that the already-open Codex Desktop chat refreshed or
  that its visible agent received a live wakeup. A running turn is recorded as
  `status: "submitted"` with `turn_status: "running"`. The `woken` label is
  retained as a v0.1 compatibility value; interpret it as completed headless
  history delivery for Codex.
- Approval prompts raised by a headless follow-up turn are auto-declined (never
  auto-approved) and recorded in the receipt as `auto_declined_requests` — no
  human is attached to the headless client. If receipts show declines, relax
  the thread's approval policy enough for read-only verification commands.
- Review the durable inbox/event/result/evidence history manually. Record that
  review without deleting any artifact with `watcher --host codex acknowledge
  --event-id EVENT_ID --reason "reviewed manually"`.
- When ending the dispatching Codex turn, show the user
  `orchestrator-engine --project-root /path/to/project worker wait --task-id
  TASK-ID`. It refreshes one compact terminal line and rings the terminal bell
  on completion without invoking a model; the user then returns to the chat
  for review.
- For supported live orchestration, prefer Claude stream as the host. VS Code
  chat is a best-effort UI path. Use `codex exec` as a worker profile; Codex
  Desktop remains useful for dispatching work when delayed/history visibility
  is acceptable.

Codex can also continue the original turn by blocking on deterministic worker
state. Prefer a direct `worker wait --json` call. A low-cost relay subagent is
useful only when native agent waiting provides a materially longer or more
reliable blocking window than the parent's command tool; the parent must still
remain active in one native wait. This is in-turn continuation, not detached
live wakeup. For unknown or long work, end the turn and show `worker wait` to
the user instead of occupying the chat indefinitely.

See [Codex in-turn continuation](codex-in-turn-continuation.md) for the verified
behavior, role boundaries, token tradeoffs and recovery rules. Do not repeatedly
ask either the parent model or a relay model for task status.

For parallel tasks, repeat `--task-id` and select `--mode all` or `--mode any`.
One aggregate wait is cheaper and easier to deduplicate than one relay per task.

## Claude Code / Claude for Windows

Delivery mechanism: the Claude harness natively wakes a session when a watched
command emits output. No push from the engine is needed — do not run a
callback service for this host.

Live status: recommended live host. The watched stream wakes the same Claude
session that armed it.

From the Claude chat you orchestrate from, arm a watch (Monitor / background
task) on:

```bash
orchestrator-engine --project-root /path/to/project watcher stream
```

Every new inbox signal is printed as one JSON line and wakes the chat. The
stream uses `watcher-claude-stream-state.json`, so each Claude signal is
delivered once and callback services for other hosts do not consume it.
Delivery is at-most-once: a signal is marked seen when its line is printed, so
if the armed watch dies at that exact moment the line is lost — check
`orchestrator-engine inbox` output against recent task results after re-arming
a watch that was down.

Check stream health:

```bash
orchestrator-engine --project-root /path/to/project watcher stream status
```

If the status is `stale` or `not_started`, re-arm `watcher stream` from the
Claude chat. Re-arming is safe because seen event ids remain in the stream
state file.

Optionally record the intent for other tooling:

```bash
orchestrator-engine --project-root /path/to/project bind --host claude
```

## VS Code Copilot

Delivery mechanism: `code chat --reuse-window "<message>"` sends the follow-up
prompt to the chat view of the last active VS Code window.

Live status: best-effort live UI delivery to the last active VS Code window,
subject to the VS Code `code chat` command and the user's active window state.

```bash
orchestrator-engine --project-root /path/to/project bind --host vscode

orchestrator-engine --project-root /path/to/project watcher \
  --host vscode --action callback service start --interval-seconds 5
```

Notes:

- The CLI targets the last active window, not a specific conversation.
- Requires a VS Code installation whose CLI exposes the documented `chat`
  subcommand and a signed-in chat provider. A version number alone is not a
  sufficient readiness check, especially across WSL/Windows wrappers.

## Multi-Host Coexistence

For callback hosts, prefer host-scoped services:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host vscode --action callback service start
```

Host-scoped callback services use separate
`watcher-<host>-callback-state.json`, service and heartbeat files. The legacy
unscoped callback service still works, but it is best treated as a single
combined callback channel for compatibility.

## Dispatching workers

Configure the CLI workers once in `.orchestrator/workers.toml` (see
[contracts.md](contracts.md)). Model and effort live in each worker's
`command`; define several profiles (fast/deep) so the orchestrating agent can
pick one per task. Then dispatch from the host chat:

```bash
orchestrator-engine --project-root /path/to/project worker run \
  --worker claude --task-id TASK-001 --prompt-file task-001.md \
  --intent-file task-001-intent.json
```

The intent records role, risk, verification breadth, permissions and explicit
commit/push/network authorizations. See the canonical intent example in the
[setup guide](setup-guide.md#step-7--end-to-end-smoke-test).

`worker run` returns immediately so the chat turn can end. A detached
supervisor runs the worker CLI, captures stdout/stderr under
`.orchestrator/tasks/TASK-001/`, writes `result.json` + `evidence.json` and
emits the standard terminal event, which triggers the configured host-specific
delivery path.
