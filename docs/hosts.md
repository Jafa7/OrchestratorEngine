# Host setup

OrchestratorEngine wakes the chat the user orchestrates from. Each host has a
different wake mechanism; the binding contract tells the watcher which one to
use.

Everything engine-side runs where the CLI workers run (typically WSL).
Windows-side actions (the Codex deep link, `code` CLI) are reached through the
normal WSL interop.

## Codex Desktop (Windows app, WSL mode)

Wake mechanism: inject a turn through a Codex App Server process, then open the
thread in the desktop app via its `codex://threads/<thread-id>` deep link.

1. In the Codex chat you orchestrate from, find the thread id.
2. Bind the project and start the watcher service:

```bash
orchestrator-engine --project-root /path/to/project bind \
  --host codex --thread-id THREAD_ID

orchestrator-engine --project-root /path/to/project watcher \
  --action callback service start --interval-seconds 5
```

Notes:

- The injected turn only reports success after the App Server confirms the
  turn completed; rate-limited or failed turns are retried with backoff.
- The deep link (`Start-Process 'codex://threads/...'` through
  `powershell.exe`) brings the thread to the foreground. If it fails, the
  receipt records `activation: "failed"` but the wakeup itself stays valid.

## Claude Code / Claude for Windows

Wake mechanism: the Claude harness natively wakes a session when a watched
command emits output. No push from the engine is needed — do not run a
callback service for this host.

From the Claude chat you orchestrate from, arm a watch (Monitor / background
task) on:

```bash
orchestrator-engine --project-root /path/to/project watcher stream
```

Every new inbox signal is printed as one JSON line and wakes the chat. The
stream shares the standard watcher state, so each signal is delivered once.

Optionally record the intent for other tooling:

```bash
orchestrator-engine --project-root /path/to/project bind --host claude
```

## VS Code Copilot

Wake mechanism: `code chat --reuse-window "<message>"` injects the wakeup
prompt into the chat view of the last active VS Code window.

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
[contracts.md](contracts.md)), then dispatch from the host chat:

```bash
orchestrator-engine --project-root /path/to/project worker run \
  --worker claude --task-id TASK-001 --prompt-file task-001.md
```

`worker run` returns immediately so the chat turn can end. A detached
supervisor runs the worker CLI, captures stdout/stderr under
`.orchestrator/tasks/TASK-001/`, writes `result.json` + `evidence.json` and
emits the standard terminal event — which is what wakes the host chat.
