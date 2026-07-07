# Setup guide

Audience: an AI agent (or a human) that was asked to connect OrchestratorEngine
to an existing project. Follow the steps in order. Every step ends with a
check; do not continue past a failing check — fix it or report the blocker to
the user.

## What you are setting up

```text
 host chat (user orchestrates here)          CLI workers (detached)
 codex desktop / claude / vscode             claude -p / codex exec / copilot
        |                                            |
        |  worker run --task-id ...                  |
        |------------------------------------------->|
        |  (chat turn ends, nothing polls)           |  works...
        |                                            |  exits
        |            .orchestrator/ (durable files)  |
        |  events/<id>.json  <--- terminal event ----|
        |  inbox/signals/<id>.json                   |
        |                                            
        |  watcher (zero-token local process)        
        |<-- wakes the host chat with a short        
        |    pointer to event/evidence/result        
```

The engine never calls model APIs. It moves files and wakes chats.

## Step 0 — Gather facts

Establish these before touching anything. Ask the user rather than guessing.

1. **Project root** — absolute path of the project to adopt.
2. **Host** — which chat does the user orchestrate from?
   - `codex` — Codex Desktop app (Windows or WSL mode)
   - `claude` — Claude Code CLI or Claude for Windows
   - `vscode` — VS Code Copilot chat
3. **Workers** — which CLI agents should execute tasks. Detect what is
   installed:

```bash
which claude; which codex; which copilot
```

Constraints: Python >= 3.11 on the machine where workers run (typically WSL on
Windows). If the host is Codex Desktop on Windows in WSL mode, everything
below runs inside WSL.

## Step 1 — Install the engine

```bash
git clone https://github.com/Jafa7/OrchestratorEngine.git
cd OrchestratorEngine
pip install .
```

Use `pip install -e .` if the user wants to track engine updates from git.
The package has zero runtime dependencies.

**Check:**

```bash
orchestrator-engine --help
```

If `orchestrator-engine` is not on PATH, use `python3 -m orchestrator_engine.cli`
everywhere below — but prefer a real install: the worker supervisor re-executes
the module with the same interpreter and must be able to import it without a
manually exported `PYTHONPATH`.

## Step 2 — Bind the host chat

Binding tells the watcher which chat to wake. Run from anywhere, against the
target project root.

### Host: codex

You need the thread id of the chat the user orchestrates from. Codex session
files embed it in their filename (`rollout-<timestamp>-<THREAD_ID>.jsonl`) and
record the working directory in the first line. If you are the agent running
*inside* that chat, find your own thread id:

```bash
ls -t ~/.codex/sessions/*/*/*/rollout-*.jsonl | head -5
head -c 300 <newest file>   # confirm "cwd" matches the project
```

Take the UUID from the matching filename, then:

```bash
orchestrator-engine --project-root /path/to/project bind \
  --host codex --thread-id THREAD_ID
```

### Host: vscode

```bash
orchestrator-engine --project-root /path/to/project bind --host vscode
```

Requires VS Code 1.127+ with the `code` CLI on PATH (`code chat --help` must
work).

### Host: claude

```bash
orchestrator-engine --project-root /path/to/project bind --host claude
```

**Check (all hosts):**

```bash
orchestrator-engine --project-root /path/to/project bind --status
```

Expect `"host"` to match, and for codex a non-empty `"target_thread_id"`.

## Step 3 — Configure workers

Create `/path/to/project/.orchestrator/workers.toml` with only the CLIs that
are actually installed. **Model and effort are encoded in `command` via each
CLI's own flags** — the engine does not interpret free-form keys like `model`
or `effort` (they are only recorded in `evidence.json` for audit). Define
several profiles per CLI so the orchestrating agent can match worker cost to
task complexity at dispatch time:

```toml
[workers.claude-fast]                      # trivial checks, small edits
enabled = true
command = ["claude", "-p", "--model", "haiku",
           "--permission-mode", "acceptEdits"]
prompt_via = "stdin"
timeout_seconds = 3600

[workers.claude-deep]                      # reviews, refactors, hard bugs
enabled = true
command = ["claude", "-p", "--model", "opus", "--effort", "xhigh",
           "--permission-mode", "acceptEdits"]
prompt_via = "stdin"
timeout_seconds = 14400

[workers.codex]
enabled = true
command = ["codex", "exec", "--json",
           "-c", "model_reasoning_effort=\"high\""]
prompt_via = "arg"
timeout_seconds = 3600

[workers.copilot]
enabled = true
command = ["copilot", "--prompt"]
prompt_via = "arg"
timeout_seconds = 3600
```

Ask the user which profiles they want (model tiers, effort levels, timeouts)
instead of inventing them.

Notes:

- `prompt_via = "arg"` appends the prompt text as the final argument;
  `"stdin"` pipes it.
- `codex exec` refuses untrusted directories. Either the user marks the
  project trusted in `~/.codex/config.toml`, or add
  `--skip-git-repo-check` to the command after confirming with the user.
- Omitted `timeout_seconds` means the worker may run for hours; the
  supervisor keeps `task.json` fresh (`last_alive_at`) while it runs.

**Check:**

```bash
orchestrator-engine --project-root /path/to/project worker list
```

Every intended worker appears with `"enabled": true`.

## Step 4 — Start the wake channel

### Hosts codex and vscode — push watcher service

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --action callback service start --interval-seconds 5
```

**Check:**

```bash
orchestrator-engine --project-root /path/to/project watcher service status
```

Expect `"status": "running"` and `"heartbeat_healthy": true` (heartbeat may
take one interval to appear).

### Host claude — stream watch, no service

Do **not** start a callback service. Instead, from the orchestrating Claude
chat, arm a persistent background watch (Monitor) on:

```bash
orchestrator-engine --project-root /path/to/project watcher stream
```

Each new signal is printed as one JSON line, which wakes the chat.

**Check:** run `watcher stream` with a pre-existing unseen signal (or after the
smoke test below) and confirm it prints one line per signal.

## Step 5 — End-to-end smoke test

Add a throwaway worker to `workers.toml`:

```toml
[workers.smoke]
enabled = true
command = ["/bin/sh", "-c", "cat; echo smoke-done"]
prompt_via = "stdin"
```

Dispatch and verify:

```bash
echo "smoke task" > /tmp/smoke-prompt.md
orchestrator-engine --project-root /path/to/project worker run \
  --worker smoke --task-id SMOKE-1 --prompt-file /tmp/smoke-prompt.md

# within a few seconds:
cat /path/to/project/.orchestrator/tasks/SMOKE-1/result.json   # terminal_status: completed
orchestrator-engine --project-root /path/to/project inbox      # contains SMOKE-1
```

Then confirm the wake actually reaches the user:

- **codex** — within one watcher interval the thread receives a
  `LOCAL_AI_ORCHESTRATOR_WAKEUP` turn and the desktop app opens it via the
  `codex://threads/...` deep link. Receipt:
  `.orchestrator/inbox/thread-wakeups/<event_id>.json` with
  `"status": "woken"`.
- **vscode** — the chat view of the last active window receives the wakeup
  message; same receipt file.
- **claude** — the armed stream prints the signal line and the chat wakes.

Afterwards remove the `smoke` worker entry. Do not delete
`.orchestrator/events/` or `inbox/signals/` — they are the audit trail.

## Step 6 — Teach the orchestrating chat

Add this to the adopted project's agent instructions file (`AGENTS.md`,
`CLAUDE.md`, or `.github/copilot-instructions.md` — whichever the host reads):

```markdown
## Orchestration with OrchestratorEngine

To delegate a task to a CLI worker:

1. Check available worker profiles: `orchestrator-engine --project-root <root> worker list`.
2. Pick the profile matching the task: cheap/fast profiles for trivial checks
   and mechanical edits, deep/high-effort profiles for reviews, refactors and
   hard bugs. The user can override your choice in chat.
3. Write the full task prompt to a file (e.g. `.orchestrator/prompts/<task-id>.md`).
4. Dispatch: `orchestrator-engine --project-root <root> worker run \
   --worker <profile> --task-id <TASK-ID> --prompt-file <file>`
5. End the turn. Do not poll; the watcher wakes this chat when workers finish.

When woken by a `LOCAL_AI_ORCHESTRATOR_WAKEUP` message:

1. Read the referenced event, result and evidence files.
2. Verify the worker's actual output (diffs, checks) before accepting it.
3. Decide the next safe action; dispatch follow-up tasks the same way.
4. Never commit or push unless the user explicitly asked.
```

Finally, tell the user what was configured: host binding, workers, watcher
state, and how to stop it (`watcher service stop`).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no binding found` on watcher start | Run Step 2; `callback` requires `binding.json`. |
| `host claude does not support callback wakeups` | Correct — use `watcher stream` (Step 4, claude). |
| Codex receipt stuck on `deferred` with a usage-limit message | Codex quota exhausted; the watcher retries with backoff automatically once limits reset. |
| Codex receipt `woken` with `turn_status: "running"` | Normal for long orchestrator turns; a background finalizer updates the receipt when the turn ends. Requires service mode (not `watcher once`). |
| Codex receipt `woken` but window did not focus | Check `activation` field in the receipt; the deep link needs `powershell.exe` reachable (WSL interop) and the desktop app installed. |
| `code chat` exits non-zero | VS Code < 1.127 or `code` not on PATH; wakeup stays retryable. |
| `worker run` → `task already exists` | Task ids are one-shot by design; pick a new id. |
| Supervisor log shows `ModuleNotFoundError: orchestrator_engine` | Engine was run via ad-hoc `PYTHONPATH` instead of being installed; run `pip install .` (Step 1). |
| Watcher status `degraded` / `crashed` | Read `.orchestrator/inbox/logs/watcher-service.log`, then `watcher service restart`. |
