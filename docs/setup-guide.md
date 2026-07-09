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
orchestrator-engine --version
```

If `orchestrator-engine` is not on PATH, use `python3 -m orchestrator_engine.cli`
everywhere below — but prefer a real install: the worker supervisor re-executes
the module with the same interpreter and must be able to import it without a
manually exported `PYTHONPATH`.

## Step 1.5 — Adopt the project layout

Run the create-only scaffolder in the project being connected:

```bash
orchestrator-engine --project-root /path/to/project adopt --host codex
```

Use `--host claude` or `--host vscode` when that is the user's host chat. The
command is idempotent: it creates missing `.orchestrator/` directories and a
disabled `workers.toml` template, but it does not write `binding.json`, enable
workers, overwrite existing files or touch durable events/signals.

**Check:**

```bash
orchestrator-engine --project-root /path/to/project doctor
```

Expect warnings until binding, workers and the wake channel are configured.

## Step 2 — Bind the host chat

Binding tells the watcher which chat to wake. Run from anywhere, against the
target project root.

### Host: codex

Run this **from inside the codex chat being bound** — the thread id is
auto-detected (from `CODEX_THREAD_ID` if set, otherwise from the most
recently modified session rollout whose recorded cwd matches the project,
which is the calling chat's own session):

```bash
orchestrator-engine --project-root /path/to/project bind --host codex
```

The output includes `thread_id_source` so you can confirm what was detected.
If detection fails (or you are binding a *different* chat), pass the id
explicitly — session filenames embed it
(`rollout-<timestamp>-<THREAD_ID>.jsonl`, first line records the cwd):

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
Run this from each chat that will dispatch work: `worker run` snapshots the
current binding into the task's `wake_target`, so completed work wakes the chat
that launched it even if another chat rebinds the same project later.

## Step 3 — Configure workers

Create `/path/to/project/.orchestrator/workers.toml` with only the CLIs that
are actually installed. Start from
[`examples/workers.toml`](../examples/workers.toml) if you want a full
fast/default/deep catalog, then enable only verified profiles. **Model, effort
and permission behavior are encoded in `command` via each CLI's own flags** —
the engine does not interpret provider flags. Free-form metadata such as
`capability`, `permission_profile`, `cost` and `recommended_for` is preserved
in evidence and helps the orchestrating agent choose a profile. Define several
profiles per CLI so the agent can match worker cost and permissions to task
complexity at dispatch time:

```toml
[workers.claude-fast]                      # trivial checks, small edits
enabled = true
command = ["claude", "-p", "--model", "haiku",
           "--permission-mode", "acceptEdits"]
prompt_via = "stdin"
expect_long_running = true
capability = "code-edit"
permission_profile = "full"
cost = "low"

[workers.claude-deep]                      # reviews, refactors, hard bugs
enabled = true
command = ["claude", "-p", "--model", "opus", "--effort", "xhigh",
           "--permission-mode", "acceptEdits"]
prompt_via = "stdin"
expect_long_running = true
capability = "code-edit"
permission_profile = "full"
cost = "high"

[workers.codex]
enabled = true
command = ["codex", "exec", "--json",
           "-c", "model_reasoning_effort=\"high\"",
           "-c", "approval_policy=\"never\"",
           "-c", "sandbox_mode=\"danger-full-access\""]
prompt_via = "arg"
expect_long_running = true
capability = "code-edit"
permission_profile = "full"
cost = "medium"

[workers.copilot]
enabled = true
command = ["copilot", "--prompt", "--allow-all", "--no-ask-user"]
prompt_via = "arg"
expect_long_running = true
capability = "code-edit"
permission_profile = "full"
cost = "medium"
```

Ask the user which profiles they want (model tiers, effort levels, timeouts)
instead of inventing them.

Notes:

- `prompt_via = "arg"` appends the prompt text as the final argument;
  `"stdin"` pipes it.
- `codex exec` refuses untrusted directories. Either the user marks the
  project trusted in `~/.codex/config.toml`, or add
  `--skip-git-repo-check` to the command after confirming with the user.
- Detached `codex exec` workers cannot handle interactive approval prompts.
  Use an explicit non-interactive policy such as
  `-c approval_policy="never"` and an intentional `sandbox_mode` in the
  worker command or in the Codex config selected by that profile.
- Detached `claude -p` workers should declare an explicit `--permission-mode`
  that matches the project's automation policy.
- Detached Copilot workers cannot answer approval prompts. Use
  `--allow-all --no-ask-user` for fully autonomous local worker profiles, or
  replace them with a narrower project-approved non-interactive policy if the
  Copilot CLI supports one. `worker list` reports warnings for known profiles
  that look interactive in detached mode.
- Omit `timeout_seconds` for AI implementation/review workers that may run for
  hours; the supervisor keeps `task.json` fresh (`last_alive_at`) while they
  run. Set `expect_long_running = true` to mark that omission as intentional.
  Add `timeout_seconds` to bounded smoke/check/script profiles.

**Check:**

```bash
orchestrator-engine --project-root /path/to/project worker list
orchestrator-engine --project-root /path/to/project worker diagnose --enabled-only
```

Every intended worker appears with `"enabled": true`. `worker diagnose` is
read-only; it reports machine-readable advisory diagnostics and exits `2` when
enabled profiles still have warnings.

## Step 4 — Configure verification workers

For long test suites, do not keep the host chat open while tests stream
output. Run checks as detached workers that write a compact verification
result, then let the watcher wake the chat.

Operational rule: dispatch the check worker from the chat that needs the
answer, then end that turn. Do not run `pytest`, `ruff` or similar long checks
directly in the host chat unless the user explicitly asked for an immediate
foreground run. The `worker run` descriptor snapshots this chat as
`wake_target`, so the completion wakeup returns to the chat that launched the
check even when several chats share the same project.

The repository includes `examples/check_runner.py` as a portable reference
runner. Copy it into the adopted project (for example
`scripts/orchestrator_check_runner.py`) or replace it with the project's
native runner if that runner writes the same contract from
[contracts.md](contracts.md#verification-result).

Example project config:

```toml
# /path/to/project/.orchestrator/checks.toml
[suites.fast]

[[suites.fast.commands]]
label = "unit"
argv = ["uv", "run", "python", "-m", "unittest", "discover",
        "-s", "tests", "-p", "test_*.py"]

[[suites.fast.commands]]
label = "ruff"
argv = ["ruff", "check", "."]

[suites.full]

[[suites.full.commands]]
label = "unit"
argv = ["uv", "run", "python", "-m", "unittest", "discover",
        "-s", "tests", "-p", "test_*.py"]

[[suites.full.commands]]
label = "ruff"
argv = ["ruff", "check", "."]

[[suites.full.commands]]
label = "diff-check"
argv = ["git", "diff", "--check"]
```

Example worker profiles:

```toml
[workers.check-fast]
enabled = true
command = ["python3", "scripts/orchestrator_check_runner.py",
           "--suite", "fast"]
prompt_via = "stdin"
timeout_seconds = 3600

[workers.check-full]
enabled = true
command = ["python3", "scripts/orchestrator_check_runner.py",
           "--suite", "full"]
prompt_via = "stdin"
timeout_seconds = 14400
```

The prompt content is ignored by the reference runner, but keeping
`prompt_via = "stdin"` lets agents dispatch checks through the same
`worker run` command as other work.

**Check:**

```bash
orchestrator-engine --project-root /path/to/project worker run \
  --worker check-fast --task-id CHECK-SMOKE-1 --prompt-file /tmp/smoke-prompt.md

cat /path/to/project/.orchestrator/checks/*/verification-result.json
orchestrator-engine --project-root /path/to/project checks --severity warning
```

If the verification result reports `"status": "passed"`, the woken agent
should read only `verification-result.json` and `summary.txt`. `checks`
summarizes all check results, failed command log paths and missing artifacts in
one compact JSON report. If a check reports `"failed"` or `"errored"`, read
`summary.txt` first, then inspect only the failed command logs referenced by
the JSON result.

## Step 5 — Start the wake channel

### Hosts codex and vscode — push watcher service

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host codex --action callback service start --interval-seconds 5
```

**Check:**

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host codex service status
```

Expect `"status": "running"` and `"heartbeat_healthy": true` (heartbeat may
take one interval to appear). Use `--host vscode` for a VS Code callback
service. Host-scoped callback services use separate state/service/heartbeat
files and can coexist with Claude stream watches.

### Host claude — stream watch, no service

Do **not** start a callback service. Instead, from the orchestrating Claude
chat, arm a persistent background watch (Monitor) on:

```bash
orchestrator-engine --project-root /path/to/project watcher stream
```

Each new signal is printed as one JSON line, which wakes the chat.
The stream consumes only `claude` wake targets and uses its own state file
(`watcher-claude-stream-state.json`), so it can coexist with a callback
service delivering Codex or VS Code signals from the same inbox.

**Check:**

```bash
orchestrator-engine --project-root /path/to/project watcher stream status
```

Expect `"status": "fresh"` while the stream is armed and scanning. Run
`watcher stream` with a pre-existing unseen signal (or after the smoke test
below) and confirm it prints one line per signal.

## Step 6 — End-to-end smoke test

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
  `codex://threads/...` deep link. On Windows, the adapter also sends a
  best-effort UI refresh pulse after activation so an already-open Codex
  Desktop thread reloads the stored turn. Receipt:
  `.orchestrator/inbox/thread-wakeups/<event_id>.json` with
  `"status": "woken"` plus `activation` and `live_refresh` fields.
- **vscode** — the chat view of the last active window receives the wakeup
  message; same receipt file.
- **claude** — the armed stream prints the signal line and the chat wakes.

Afterwards remove the `smoke` worker entry. Do not delete
`.orchestrator/events/` or `inbox/signals/` — they are the audit trail.

## Step 7 — Teach the orchestrating chat

Add this to the adopted project's agent instructions file (`AGENTS.md`,
`CLAUDE.md`, or `.github/copilot-instructions.md` — whichever the host reads):

```markdown
## Orchestration with OrchestratorEngine

To delegate a task to a CLI worker:

0. Make sure the binding targets THIS chat (`bind --status`); if you are a
   new chat, rebind yourself first (`bind --host codex` auto-detects your
   thread; claude/vscode need no id). The binding is snapshotted into each
   task at dispatch time, so multiple chats may safely share one project as
   long as each chat binds itself before dispatching work.
1. Check available worker profiles: `orchestrator-engine --project-root <root> worker list`.
2. Pick the profile matching the task: cheap/fast profiles for trivial checks
   and mechanical edits, deep/high-effort profiles for reviews, refactors and
   hard bugs. The user can override your choice in chat.
3. Write the full task prompt to a file (e.g. `.orchestrator/prompts/<task-id>.md`).
4. Dispatch: `orchestrator-engine --project-root <root> worker run \
   --worker <profile> --task-id <TASK-ID> --prompt-file <file>`
5. End the turn. Do not poll; the watcher wakes this chat when workers finish.
   If you must inspect detached task state before wakeup, run
   `orchestrator-engine --project-root <root> worker tasks --severity warning`
   and read only the reported artifacts.

When woken by a `LOCAL_AI_ORCHESTRATOR_WAKEUP` message:

1. Read the referenced event, result and evidence files.
2. If the worker produced an `ORCHESTRATOR_VERIFICATION_RESULT`, read its
   `verification-result.json` and `summary.txt` first. Do not read full logs
   for a passed check unless the user asks; for failed checks, inspect only
   the failed command logs referenced by the result.
3. Verify the worker's actual output (diffs, checks) before accepting it.
4. Decide the next safe action; dispatch follow-up tasks the same way.
5. Never commit or push unless the user explicitly asked.
```

Finally, tell the user what was configured: host binding, workers, watcher
state, and how to stop it (`watcher --host HOST service stop` for callback
hosts, or by ending the armed stream command for Claude).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no binding found` on watcher start | Run Step 2; `callback` requires `binding.json`. |
| `host claude does not support callback wakeups` | Correct — use `watcher stream` (Step 4, claude). |
| Codex receipt stuck on `deferred` with a usage-limit message | Codex quota exhausted. New watcher versions classify this as `deferred_manual_required` and stop automatic retries; read event/result/evidence manually, then run `orchestrator-engine --project-root <root> watcher acknowledge --event-id <event-id> --reason "read manually"` or `watcher deferred retry --event-id <event-id> --reason "quota reset"` after quota resets. |
| `watcher service status` shows `deferred_manual_required` | The watcher stopped retrying a callback that needs operator action. Run `watcher deferred list`, inspect event/result/evidence, then `watcher deferred retry --event-id ...` or `watcher acknowledge --event-id ...`. |
| Bare `watcher service status` disagrees with `doctor` | Host-scoped callback services use host-specific state files. Run `watcher --host <host> service status` for the active callback channel shown by `bind --status` or `doctor`. |
| `watcher stream status` is `stale` or `not_started` | Re-arm `watcher stream` from the Claude chat. Re-arming is safe: the stream state keeps seen event ids, so already delivered signals are not repeated. |
| `watcher stream status` is `erroring` | The stream loop is alive but the latest scan failed. Inspect `last_error`, fix the inbox/state issue, and keep the stream running; the status returns to `fresh` after a successful scan. |
| Codex receipt `deferred` with `thread_active` or `thread_recently_active` | Normal guard: the worker finished while the target chat was still active or had just written to its rollout. End the orchestrating turn; watcher retries with backoff instead of injecting a parallel turn. The recent-activity grace window is short (30 seconds by default), so this trades a small delay for avoiding concurrent injected turns. |
| Codex receipt `woken` with `turn_status: "running"` | Normal for long orchestrator turns; a background finalizer updates the receipt when the turn ends. Requires service mode (not `watcher once`). |
| Codex receipt `woken` but window did not focus | Check `activation` field in the receipt; the deep link needs `powershell.exe` reachable (WSL interop) and the desktop app installed. |
| Codex receipt `woken`, window focused, but no new visible turn | Check `live_refresh`; the turn was delivered to Codex storage, but Codex Desktop may not have reloaded the already-open thread. On Windows the adapter sends a best-effort `Ctrl+R` refresh pulse after deep-link activation. |
| `code chat` exits non-zero | VS Code < 1.127 or `code` not on PATH; wakeup stays retryable. |
| `worker run` → `task already exists` | Task ids are one-shot by design; pick a new id. |
| Verification worker passed but logs are huge | Read `.orchestrator/checks/<check_id>/summary.txt`; full logs are durable artifacts and do not need to be pasted into chat. |
| Verification worker failed | Read `verification-result.json`, then only the command logs referenced by failed command entries. |
| Multiple verification runs need triage | Run `checks --severity warning`; inspect `summary_path` and `failed_commands[].log_path` from the JSON output. |
| Worker appears stuck or no wakeup arrived | Run `worker tasks --severity warning` to inspect stale heartbeats, dead supervisor/worker pids and missing artifacts before reading full logs. |
| Copilot worker stalls with `Permission denied and could not request permission from user` | The profile is interactive. Add autonomous Copilot flags such as `--allow-all --no-ask-user`, or configure a project-approved narrower non-interactive policy. |
| Supervisor log shows `ModuleNotFoundError: orchestrator_engine` | Engine was run via ad-hoc `PYTHONPATH` instead of being installed; run `pip install .` (Step 1). |
| Watcher status `degraded` / `crashed` | Read `.orchestrator/inbox/logs/watcher-service.log`, then `watcher service restart`. |
