# Host setup

OrchestratorEngine routes worker completions back to the chat the user
orchestrates from. Each host has a different wake mechanism; the binding
contract tells the watcher which one to use. Distinguish durable delivery from
live wakeup:

- **Durable delivery** means the completion is written into the target host's
  history or inbox and the audit trail points to event/result/evidence.
- **Live wakeup** means the already-open host chat receives the message and
  the active agent continues in that same visible session.

Everything engine-side runs where the CLI workers run (typically WSL).
Windows-side actions (the Codex deep link, `code` CLI) are reached through the
normal WSL interop.

## Codex Desktop (Windows app, WSL mode)

Wake mechanism: inject a turn through a Codex App Server process, then open the
thread in the desktop app via its `codex://threads/<thread-id>` deep link.

Live status: durable delivery only on Windows Desktop. The injected turn is
handled by an App Server/headless engine and written to Codex thread storage.
The already-open Desktop chat does not reliably wake as the same live agent;
the new turn may become visible only after thread switch, reload, restart or
delayed UI refresh. Treat the deep link and `live_refresh` fields as
best-effort focus/refresh diagnostics, not proof that the visible Desktop
agent woke.

1. In the Codex chat you orchestrate from, find the thread id.
2. Bind the project and start the watcher service:

```bash
orchestrator-engine --project-root /path/to/project bind \
  --host codex --thread-id THREAD_ID

orchestrator-engine --project-root /path/to/project watcher \
  --action callback service start --interval-seconds 5
```

Notes:

- Rate-limited or immediately failing turns are detected within a 2-minute
  failure window and retried with backoff. Turns still running after the
  window (orchestrator reviews may take hours) are reported `woken` with
  `turn_status: "running"` and finalized in the background — use the service
  mode above, not `watcher once`, so the finalizer has a long-lived process.
- Stopping the watcher service also stops in-flight wakeup turns it started
  (they run inside App Server processes in the service's process group).
- Approval prompts raised by a wakeup turn are auto-declined (never
  auto-approved) and recorded in the receipt as `auto_declined_requests` — no
  human is attached to the injected client. If receipts show declines, relax
  the thread's approval policy enough for read-only verification commands.
- The deep link (`Start-Process 'codex://threads/...'` through
  `powershell.exe`) brings the thread to the foreground. If it fails, the
  receipt records `activation: "failed"` but the wakeup itself stays valid.
- Codex Desktop may keep an already-open thread in memory after an external
  App Server turn lands in session storage. On Windows, the adapter sends a
  best-effort `Ctrl+R` refresh pulse after deep-link activation; receipts record
  this as `live_refresh` / `live_refresh_strategy`.
- For live orchestration, prefer Claude stream or VS Code chat as the host and
  use `codex exec` as a worker profile. Codex Desktop remains useful for
  dispatching work when delayed/history visibility is acceptable.

## Claude Code / Claude for Windows

Wake mechanism: the Claude harness natively wakes a session when a watched
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
stream shares the standard watcher state, so each signal is delivered once.
Delivery is at-most-once: a signal is marked seen when its line is printed, so
if the armed watch dies at that exact moment the line is lost — check
`orchestrator-engine inbox` output against recent task results after re-arming
a watch that was down.

Optionally record the intent for other tooling:

```bash
orchestrator-engine --project-root /path/to/project bind --host claude
```

## VS Code Copilot

Wake mechanism: `code chat --reuse-window "<message>"` injects the wakeup
prompt into the chat view of the last active VS Code window.

Live status: live UI injection into the last active VS Code window, subject to
the VS Code `code chat` command and the user's active window state.

```bash
orchestrator-engine --project-root /path/to/project bind --host vscode

orchestrator-engine --project-root /path/to/project watcher \
  --action callback service start --interval-seconds 5
```

Notes:

- The CLI targets the last active window, not a specific conversation.
- Requires VS Code with the `chat` CLI subcommand (1.127+).

## Dispatching workers

Configure the CLI workers once in `.orchestrator/workers.toml` (see
[contracts.md](contracts.md)). Model and effort live in each worker's
`command`; define several profiles (fast/deep) so the orchestrating agent can
pick one per task. Then dispatch from the host chat:

```bash
orchestrator-engine --project-root /path/to/project worker run \
  --worker claude --task-id TASK-001 --prompt-file task-001.md
```

`worker run` returns immediately so the chat turn can end. A detached
supervisor runs the worker CLI, captures stdout/stderr under
`.orchestrator/tasks/TASK-001/`, writes `result.json` + `evidence.json` and
emits the standard terminal event — which is what wakes the host chat.
