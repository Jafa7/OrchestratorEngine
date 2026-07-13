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
        |  host delivery channel (zero-token local process)
        |<-- routes a short pointer to
        |    event/evidence/result
```

The engine never calls model APIs. It moves files and invokes the configured
local host delivery channel. Live wakeup support depends on that host.

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
command -v claude
command -v codex
command -v copilot
```

Constraints: Python >= 3.11 on the machine where workers run (typically WSL on
Windows). If the host is Codex Desktop on Windows in WSL mode, everything
below runs inside WSL.

## Step 1 — Install the engine

```bash
git clone https://github.com/Jafa7/OrchestratorEngine.git
cd OrchestratorEngine
python -m pip install .
```

Use `python -m pip install -e .` if the user wants to track engine updates
from git. For development and schema conformance tests, install
`python -m pip install -e '.[test]'`. The package has zero runtime dependencies.

For a reproducible install outside a source checkout, install the release tag:

```bash
python -m pip install \
  "orchestrator-engine @ git+https://github.com/Jafa7/OrchestratorEngine.git@v0.4.1"
```

GitHub Release archives and wheel/sdist assets are published with the tag;
OrchestratorEngine is not currently published to PyPI.

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
disabled `workers.toml` template plus a provider-neutral worker policy, but it
does not write `binding.json`, enable workers, overwrite existing files or
touch durable events/signals.

Before dispatching real work, confirm the adopting project's public Git policy
for local runtime state. Effective prompts contain the task text and are stored
durably under `.orchestrator/tasks/`; worker logs and evidence may also contain
private data. Normally the whole `.orchestrator/` runtime directory is ignored
and only a sanitized example config is committed elsewhere. If the project
intentionally versions part of the directory, add precise ignore rules for
`tasks/`, `events/`, `inbox/`, `checks/`, `prompts/` and private policy files.
Do not invent a backup or retention destination; follow the adopting project's
explicit local-state policy.

**Check:**

```bash
orchestrator-engine --project-root /path/to/project doctor
git -C /path/to/project status --short -- .orchestrator
```

Expect warnings until binding, workers and the delivery channel are configured.
The Git command must not show runtime artifacts that would enter public Git.

## Step 2 — Bind the host chat

Binding tells the watcher which host target owns a completion. Run from
anywhere, against the target project root.

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

Requires a VS Code installation whose `code` CLI exposes the documented
`chat` subcommand and a signed-in chat provider. Check the actual CLI reached
from the worker environment; a version number alone does not prove that a
WSL/Windows wrapper routes `code chat` correctly.

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
current binding into the task's `wake_target`, so completed work is routed to
the host target that launched it even if another chat rebinds the same project
later.

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
[policies.quality-efficient]
files = ["policies/quality-efficient.md"]
quality_priority = "correctness-first"
context_strategy = "progressive"
verification_strategy = "risk-based-final-gate"
output_strategy = "compact-evidence"

[workers.claude-fast]                      # trivial checks, small edits
enabled = true
command = ["claude", "-p", "--model", "haiku",
           "--permission-mode", "dontAsk"]
prompt_via = "stdin"
policy = "quality-efficient"
expect_long_running = true
capability = "code-edit"
permission_profile = "restricted"
cost = "low"

[workers.claude-deep]                      # reviews, refactors, hard bugs
enabled = true
command = ["claude", "-p", "--model", "opus", "--effort", "xhigh",
           "--dangerously-skip-permissions"]
prompt_via = "stdin"
policy = "quality-efficient"
expect_long_running = true
capability = "code-edit"
permission_profile = "full"
cost = "high"

[workers.codex]
enabled = true
command = ["codex", "exec", "--json", "-m", "gpt-5.6-terra",
           "-c", "model_reasoning_effort=\"high\"",
           "-c", "approval_policy=\"never\"",
           "-c", "sandbox_mode=\"danger-full-access\""]
prompt_via = "arg"
policy = "quality-efficient"
expect_long_running = true
capability = "code-edit"
permission_profile = "full"
cost = "medium"

[workers.copilot]
enabled = true
command = ["copilot", "--model", "auto", "--effort", "high",
           "--allow-all", "--no-ask-user", "--prompt"]
prompt_via = "arg"
policy = "quality-efficient"
expect_long_running = true
capability = "code-edit"
permission_profile = "full"
cost = "medium"
```

Ask the user which profiles they want (model tiers, effort levels, timeouts)
instead of inventing them.

`adopt` creates `policies/quality-efficient.md` next to `workers.toml`. Assign
it explicitly to AI profiles with `policy = "quality-efficient"`. If adopting
an existing layout or copying `examples/workers.toml`, also copy
[`examples/policies/quality-efficient.md`](../examples/policies/quality-efficient.md)
to `.orchestrator/policies/quality-efficient.md`. The policy is
correctness-first: it reduces repeated reads, broad exploration, intermediate
full suites and oversized handoffs, while requiring deeper investigation for
high-risk or uncertain work.
See [worker behavior policies](worker-policies.md) before adding project- or
role-specific overlays.

Notes:

- Current Codex GPT-5.6 roles are Sol for quality-first complex work, Terra for
  balanced everyday work, and Luna for efficient high-volume work. Model
  availability depends on the installed Codex client and account, so verify a
  profile before enabling it.
- `prompt_via = "arg"` appends the prompt text as the final argument;
  `"stdin"` pipes it.
- `policy` selects a `[policies.*]` bundle. The engine snapshots the selected
  files and the task prompt before supervisor spawn and records both hashes in
  task/evidence. Do not point policy bundles at private planning documents or
  use them as a substitute for project-specific `AGENTS.md` instructions.
- `codex exec` refuses untrusted directories. Either the user marks the
  project trusted in `~/.codex/config.toml`, or add
  `--skip-git-repo-check` to the command after confirming with the user.
- Detached `codex exec` workers cannot handle interactive approval prompts.
  Use an explicit non-interactive policy such as
  `-c approval_policy="never"` and an intentional `sandbox_mode` in the
  worker command or in the Codex config selected by that profile.
- Detached `claude -p` workers should declare an explicit `--permission-mode`
  that matches the project's automation policy. `dontAsk` avoids interactive
  prompts but may deny restricted tools. `--dangerously-skip-permissions`
  provides full autonomy and must be limited to a trusted project with an
  appropriate external security boundary.
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

For the bundled `quality-efficient` policy, inspect the `policies` and
`policy_diagnostics` fields. `status: current` means the local file matches the
installed bundled revision. `policy_update_available` means the files differ;
review the hashes and compare the files before deciding whether to preserve a
local customization or copy the newer bundled policy. No command overwrites
the local policy automatically.

If the adopter configured `availability_probe` for a worker, run it explicitly
with `worker availability --worker NAME`. Use
`--availability-mode block-unavailable` (or legacy
`--preflight-availability`) to block a known unavailable result. Use
`--availability-mode require-available` when a missing, failed or unavailable
probe must fail closed. Existing profiles remain compatible because the
default mode is `off`.

For structured admission, set `[dispatch].intent_enforcement = "strict"` and
add `[workers.NAME.admission]` declarations for roles, maximum risk,
verification levels and commit/push/network authorizations. Strict mode also
uses the existing `permission_profile`. Treat these values as project-owned
assertions for deterministic profile selection, not provider capability
detection.

## Step 4 — Configure verification workers

Adopt the [risk-based verification policy](verification-policy.md) before
configuring suites. Each project should define structural, focused and full
checks using its native tools, and place the reusable instruction snippet from
that policy in its `AGENTS.md`, `CLAUDE.md` or equivalent. A `fast` suite is
not automatically a focused suite: it is focused only when it covers the
touched behavior without running unrelated tests.

For long test suites, do not keep the host chat open while tests stream
output. Run checks as detached workers that write a compact verification
result, then let the configured host channel deliver the follow-up.

Operational rule: dispatch the check worker from the chat that needs the
answer, then end that turn. Do not run `pytest`, `ruff` or similar long checks
directly in the host chat unless the user explicitly asked for an immediate
foreground run. The `worker run` descriptor snapshots this chat as
`wake_target`, so the completion is routed to the host target that launched the
check even when several chats share the same project.

Use focused checks while implementation is still changing. Dispatch the full
suite only after the work is otherwise complete and needs final verification.
If it fails, use the failed-command evidence and focused checks while fixing,
then dispatch full again only for the new final candidate.

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

If the verification result reports `"status": "passed"`, the receiving agent
should read only `verification-result.json` and `summary.txt`. `checks`
summarizes all check results, failed command log paths and missing artifacts in
one compact JSON report. If a check reports `"failed"` or `"errored"`, read
`summary.txt` first, then inspect only the failed command logs referenced by
the JSON result.

Do not assign a model to merely wait for the suite. The check profile should
run the deterministic project runner directly. For a passing suite no AI
triage is needed. For a failure, an optional low-cost analysis worker may read
only the referenced failed-command logs and prepare a bounded handoff for the
host agent, which verifies the diagnosis before changing code.

Use the prompt templates in [`examples/prompts`](../examples/prompts) for
review, implementation, verification and adopter-report workers. They encode
the same risk and output-economy rules: select structural/focused/full before
running checks, keep compact summaries first, use durable artifact paths
instead of full logs, and include tiny excerpts only when needed to identify a
failure.

## Step 5 — Start the delivery channel

### Host vscode — push watcher service

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host vscode --action callback service start --interval-seconds 5
```

**Check:**

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host vscode service status
```

Expect `"status": "running"` and `"heartbeat_healthy": true` (heartbeat may
take one interval to appear). Host-scoped callback services use separate
state/service/heartbeat files and can coexist with Claude stream watches.

### Host codex — history-only manual review

Do **not** start a Codex callback watcher for live refresh. A completed headless
App Server receipt is durable history, not proof that the open Desktop chat
refreshed. Use `inbox` to locate the event/result/evidence artifacts, review
them manually, then acknowledge the signal with `watcher --host codex
acknowledge --event-id EVENT_ID --reason "..."`.

### Host claude — stream watch, no service

Do **not** start a callback service. Instead, from the orchestrating Claude
chat, arm a persistent background watch (Monitor) on:

```bash
orchestrator-engine --project-root /path/to/project watcher stream
```

Each new signal is printed as one JSON line, which wakes the chat.
The stream consumes only `claude` wake targets and uses its own state file
(`watcher-claude-stream-state.json`), so it can coexist with a callback
service delivering VS Code signals from the same inbox.

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

Then confirm the correct delivery behavior:

- **codex** — review the durable inbox/event/result/evidence history manually.
  If an explicitly configured history callback produces a `"status": "woken"`
  receipt, it confirms only that its headless App Server turn completed; it
  does not prove the visible Desktop chat refreshed.
- **vscode** — the chat view of the last active window receives the follow-up
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
5. Do not perform AI polling. When ending a Codex turn, show the user this
   command before the handoff:

   ```bash
   orchestrator-engine --project-root <root> worker wait --task-id <TASK-ID>
   ```

   In an interactive terminal it refreshes one compact line, uses color and a
   terminal bell when available, and tells the user when to return to the chat.
   Its local state reads do not call a model. Use `--json` for one bounded
   machine-readable terminal result. A red `ACTION` result means the worker
   heartbeat/lease/result needs chat review; the command does not modify or
   reap it. Claude supports live stream wakeup and VS Code attempts best-effort
   UI delivery; neither needs this manual fallback.
   For diagnostics after an unsuccessful result, run `worker tasks --task-id
   <TASK-ID> --severity warning` and read only the reported artifacts.

   If the Codex turn should remain active for bounded work, prefer one direct
   `worker wait --json`. Use a low-cost relay subagent only when native agent
   waiting offers a materially better blocking window, and keep the parent in
   one native wait until the relay returns. A relay must not edit, test, review
   or poll repeatedly. See [Codex in-turn continuation](codex-in-turn-continuation.md).
   For parallel workers, repeat `--task-id` and use `--mode all` to wait for the
   full set or `--mode any` to return on the first terminal result.

If setup or runtime diagnostics still look wrong, create a structured report
for OrchestratorEngine instead of pasting full logs:

```bash
orchestrator-engine --project-root <root> \
  report draft --project-name PROJECT > /tmp/orchestrator-report.md
```

See [operator-reporting.md](operator-reporting.md).

When a `LOCAL_AI_ORCHESTRATOR_WAKEUP` follow-up message arrives:

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
| `host claude does not support callback wakeups` | Correct — use `watcher stream` (Step 5, claude). |
| Codex receipt stuck on `deferred` with a usage-limit message | Codex quota exhausted. Read event/result/evidence manually, then run `orchestrator-engine --project-root <root> watcher --host codex acknowledge --event-id <event-id> --reason "read manually"` or `watcher --host codex deferred retry --event-id <event-id> --reason "quota reset"` after quota resets. |
| `watcher service status` shows `deferred_manual_required` | The watcher stopped retrying a callback that needs operator action. Run `watcher --host <host> deferred list`, inspect event/result/evidence, then `watcher --host <host> deferred retry --event-id ... --reason "..."` or `watcher --host <host> acknowledge --event-id ... --reason "..."`. |
| Bare `watcher service status` disagrees with `doctor` | Host-scoped callback services use host-specific state files. Run `watcher --host <host> service status` for the active callback channel shown by `bind --status` or `doctor`. |
| `watcher stream status` is `stale` or `not_started` | Re-arm `watcher stream` from the Claude chat. Re-arming is safe: the stream state keeps seen event ids, so already delivered signals are not repeated. |
| `watcher stream status` is `erroring` | The stream loop is alive but the latest scan failed. Inspect `last_error`, fix the inbox/state issue, and keep the stream running; the status returns to `fresh` after a successful scan. |
| Codex receipt `deferred` with `thread_active` or `thread_recently_active` | Normal guard: the worker finished while the target chat was still active or had just written to its rollout. End the orchestrating turn; watcher retries with backoff instead of submitting a parallel headless turn. The recent-activity grace window is short (30 seconds by default), so this trades a small delay for avoiding concurrent turns. |
| Codex receipt `submitted` with `turn_status: "running"` | Normal for a long headless App Server turn; a background finalizer updates the receipt when it ends. `woken` is reserved for a completed turn and still does not prove the open Desktop chat refreshed. |
| Codex receipt `woken` but window did not focus | Check `activation` field in the receipt; the deep link needs `powershell.exe` reachable (WSL interop) and the desktop app installed. |
| Codex receipt `woken`, but no new visible Desktop turn | Expected limitation: it means a headless App Server turn completed, not that the open Desktop chat refreshed. Review durable event/result/evidence history manually and acknowledge it with the host-scoped command. |
| `code chat` exits non-zero | The reached `code` CLI may lack the documented `chat` subcommand, WSL interop may resolve the wrong wrapper, the chat provider may not be signed in, or no usable window may be active. Check `code --version`, the resolved executable and the host's official chat CLI behavior; delivery stays retryable. |
| `worker run` → `task already exists` | Task ids are one-shot by design; pick a new id. |
| Historical failed task keeps `status` or `worker tasks` noisy after a successful rerun | Preserve the task artifacts and write an operator resolution: `orchestrator-engine --project-root <root> worker resolve --task-id <old-task> --status superseded --superseded-by-task-id <new-task> --reason "successful rerun"`. Use `--status acknowledged` for a manually reviewed task that was not superseded by another task. |
| Completed Claude plan-mode task keeps `claude_plan_output_may_be_external` after its durable output was verified | Preserve the task and record a scoped acknowledgement: `orchestrator-engine --project-root <root> worker resolve --task-id <task-id> --status acknowledged --diagnostic-code claude_plan_output_may_be_external --reason "complete durable output inspected"`. This retains the diagnostic as `info`; it does not hide errors. |
| Historical worker handoff lacks `schema_version` because it followed the pre-v0.3.2 generated prompt | Inspect and preserve the original, then run `orchestrator-engine --project-root <root> artifact resolve --path <handoff-path> --reason "known historical prompt defect reviewed"`. The hash-bound companion clears only that exact malformed-metadata finding; changed bytes, unreadable JSON, invalid companions and real unsupported versions remain visible. |
| Superseded task still contributes a stale historical profile warning | Preserve the supersession relationship and replace the resolution with the same `--status superseded --superseded-by-task-id <new-task>`, plus the exact `--diagnostic-code <code>`, a new reason and `--replace`. Matching non-error diagnostics remain visible at `info`; errors are never downgraded. |
| Verification worker passed but logs are huge | Read `.orchestrator/checks/<check_id>/summary.txt`; full logs are durable artifacts and do not need to be pasted into chat. |
| Verification worker failed | Read `verification-result.json`, then only the command logs referenced by failed command entries. |
| `worker tasks` reports `task_large_worker_log` | The task may still be successful, but its stdout/stderr/supervisor logs are too large for chat. Read `result.json` and `evidence.json` first, then targeted log tails only. Tune the threshold with `--large-log-bytes`. |
| Multiple verification runs need triage | Run `checks --severity warning`; inspect `summary_path` and `failed_commands[].log_path` from the JSON output. |
| Worker appears stuck or no follow-up arrived | Run `worker tasks --severity warning` to inspect stale heartbeats, dead supervisor/worker pids and missing artifacts before reading full logs. |
| Copilot worker stalls with `Permission denied and could not request permission from user` | The profile is interactive. Add autonomous Copilot flags such as `--allow-all --no-ask-user`, or configure a project-approved narrower non-interactive policy. |
| Supervisor log shows `ModuleNotFoundError: orchestrator_engine` | Engine was run via ad-hoc `PYTHONPATH` instead of being installed; run `pip install .` (Step 1). |
| Watcher status `degraded` / `crashed` | Read `.orchestrator/inbox/logs/watcher-service.log`, then `watcher service restart`. |
