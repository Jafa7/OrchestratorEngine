# Host setup

OrchestratorEngine routes worker completions back to the host target the user
orchestrates from. Each host has a different delivery mechanism; the binding
contract tells the watcher which one to use. Distinguish durable delivery from
live wakeup:

- **Durable delivery** means the completion is written into the target host's
  history or inbox and the audit trail points to event/result/evidence.
- **Live wakeup** means the already-open host chat receives the message and
  the active agent continues in that same visible session.

Everything engine-side runs where the CLI workers run. In WSL, Windows-side
actions (`codex.exe`, the Codex deep link, and the `code` CLI) are reached
through normal WSL interop.

Machine-readable capabilities are available with
`orchestrator-engine host-capabilities`:

| Host | `delivery_mode` | `live_refresh_support` |
| --- | --- | --- |
| Claude | `session_stream` | `supported` |
| VS Code | `ui_injection` | `best_effort` |
| Codex Desktop | `session_queue` | `supported` |

This is a versioned report with `schema_version`, `kind`, `host_count` and a
bounded, stable `hosts` collection. Codex also declares its `codex queue`
requirement and the `headless_app_server_turn` / `unsupported` fallback. These
describe message delivery, not deep-link or window activation success.

`ui_injection` is a stable machine-readable v0.1 identifier for invoking the
documented VS Code chat CLI. It does not mean that the engine bypasses host
security. All adapters use user-installed local CLIs or interfaces under the
user's account and an explicit project binding; OrchestratorEngine does not
access provider accounts directly or bypass authentication.

## Codex Desktop

Preferred delivery mechanism: `codex queue --thread THREAD --message TEXT`.
The command submits the bounded wakeup to the shared local App Server daemon
that owns the live Desktop task. If another turn is active, the message waits
in the host queue instead of starting a parallel headless turn.

Required capability:

```bash
codex queue --help
```

The command must expose both `--thread` and `--message`. Under WSL, bind records
the Windows `codex.exe` launcher for Windows-owned Desktop tasks so the watcher
reaches the same daemon rather than a separate WSL session store. Desktop app
updates replace their versioned executable directory; if a snapshotted managed
path no longer exists, the adapter resolves the newest installed launcher at
delivery time without rewriting the task's audit snapshot.

1. Run the bind command from the Codex chat that will dispatch work. The engine
   auto-detects that chat's thread id:

```bash
orchestrator-engine --project-root /path/to/project bind --host codex
```

Confirm `thread_id_source` and `target_thread_id` in the output. Use explicit
`--thread-id THREAD_ID` only when auto-detection fails or an operator is binding
a different chat.

2. Start the host-scoped callback service:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host codex --action callback service start --interval-seconds 5
```

3. Verify the channel:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host codex service status
```

Notes:

- `status: "queued"` means the live daemon acknowledged the message and the
  receipt contains `queue_message_id`. It does not claim that the subsequent
  agent turn has already completed.
- A timeout, nonzero process exit, or successful exit without a matching
  acknowledgement is `queue_delivery_ambiguous`. The watcher requires manual
  review before retry; a blind retry could enqueue a duplicate message.
- If `codex queue` is unavailable, the adapter falls back to the previous
  headless App Server turn. Its receipts retain
  `delivery_mode: "headless_app_server_turn"`; `woken` then means that headless
  turn completed, not that the visible Desktop task woke.
- Approval prompts in the headless fallback are auto-declined, never approved,
  and recorded as `auto_declined_requests` because no human is attached to that
  client.
- The queued message contains only deterministic pointers to durable
  event/result/evidence artifacts. Worker output remains data, not instructions.

Codex can also continue the original active turn by blocking on deterministic
worker state. Prefer a direct `worker wait --json` call. A low-cost relay
subagent is useful only when native agent waiting provides a materially longer
or more reliable blocking window than the parent's command tool. For unknown
or long work, end the turn and let the callback service queue the completion.

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
