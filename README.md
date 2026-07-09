# OrchestratorEngine

OrchestratorEngine is a small event-driven coordination layer for AI worker
processes. A user orchestrates from a host chat (Claude Code / Claude for
Windows, VS Code Copilot, or Codex Desktop with the limitation documented
below), dispatches tasks to CLI workers, and ends the turn. Workers run
detached, write a terminal event to disk when they finish, and a local watcher
routes the completion back to the dispatching chat — without API keys and
without token-spending heartbeat prompts.

Supported host/worker combinations are symmetric: any host chat can manage any
CLI workers (Claude, Codex, Copilot, or any other command-line worker).
Long verification runs can use the same flow: run checks detached, keep full
logs as artifacts, and wake the chat with a compact pass/fail summary.

Host wakeup quality is provider-specific. Claude stream wakeups are the
recommended live orchestration path today: the already-open Claude chat wakes
and continues in that session. VS Code uses its chat CLI. Codex Desktop on
Windows can receive durable delivery into thread history and best-effort
window focus/refresh, but it does not currently provide a reliable live wakeup
channel for the already-open Desktop agent. Codex remains fully supported as a
CLI worker through `codex exec`.

## Connecting the engine to your project

**If you are an AI agent** asked to set this up: follow
[docs/setup-guide.md](docs/setup-guide.md) step by step. It contains every
command, a check after each step, and a troubleshooting table. Do not improvise
a different layout — the file contract is what makes wakeups work.

**If you are a human**: paste this to the agent in the chat you orchestrate
from:

```text
Connect OrchestratorEngine to this project.
Repository: https://github.com/Jafa7/OrchestratorEngine
Read docs/setup-guide.md in that repository and follow it exactly.
I orchestrate from this chat; ask me anything the guide says to ask.
```

## Goals

- Run workers detached from the active orchestrator turn.
- Store terminal events and inbox signals as durable JSON files.
- Wake the host chat with a bounded pointer to event/evidence/result.
- Avoid token-spending heartbeat prompts.
- Keep provider integrations at explicit adapter boundaries.
- Provide service-style watcher control: start, status, stop and restart.

## Non-goals

- This is not an AI agent runtime.
- This does not own product-specific task contracts.
- This does not replace Codex, Claude, Copilot or project-local review logic.
- This does not use provider API keys for orchestration.

## How it fits together

1. **Bind** the project to the host chat once
   (`bind --host codex|claude|vscode`).
2. **Dispatch** tasks from the host chat (`worker run`), which returns
   immediately; a detached supervisor runs the worker CLI and emits a terminal
   event on exit.
3. **Wake**: a watcher service (`--action callback`) pushes a wakeup to the
   bound host — or, for Claude, the session itself watches `watcher stream`.
   Callback services can be scoped with `watcher --host codex|vscode` so
   multiple host channels can share one inbox without consuming each other's
   signals.

Per-host setup details: [docs/hosts.md](docs/hosts.md).

Release and upgrade notes:
[CHANGELOG.md](CHANGELOG.md), [LICENSE](LICENSE), and
[docs/upgrade-guide.md](docs/upgrade-guide.md).

## File layout inside an adopted project

By default the orchestrator writes under `.orchestrator/` in the target
project:

```text
.orchestrator/
  workers.toml
  events/
    <event_id>.json
  tasks/
    <task_id>/
      task.json
      worker-stdout.log
      worker-stderr.log
      result.json
      evidence.json
      supervisor.log
  checks/
    <check_id>/
      verification-result.json
      summary.txt
      full.log
      <command-label>.log
  inbox/
    binding.json
    signals/
      <event_id>.json
    notifications/
      <event_id>.json
    thread-wakeups/
      <event_id>.json
    logs/
      watcher-service.log
    watcher-state.json
    watcher-service.json
    watcher-heartbeat.json
    watcher-<host>-callback-state.json
    watcher-<host>-callback-service.json
    watcher-<host>-callback-heartbeat.json
    watcher-claude-stream-state.json
```

The core package is project-neutral. A project may wrap it and choose a
different state directory, but the directory must still follow the
OrchestratorEngine contract. Product-specific legacy layouts should be adapted
by the product, not by OrchestratorEngine core.

## Quick start

Create the local orchestration layout in the project:

```bash
orchestrator-engine --project-root /path/to/project adopt --host codex
```

Bind the project to the host chat (example: Codex Desktop):

```bash
orchestrator-engine --project-root /path/to/project bind \
  --host codex --thread-id THREAD_ID
```

Configure workers in `/path/to/project/.orchestrator/workers.toml`:

```toml
[workers.claude]
enabled = true
command = ["claude", "-p", "--permission-mode", "acceptEdits"]
prompt_via = "stdin"
expect_long_running = true
permission_profile = "full"

[workers.codex]
enabled = true
command = ["codex", "exec", "--json",
           "-c", "approval_policy=\"never\"",
           "-c", "sandbox_mode=\"danger-full-access\""]
prompt_via = "arg"
expect_long_running = true
permission_profile = "full"
```

For a fuller fast/default/deep profile catalog with autonomous and restricted
examples, start from [examples/workers.toml](examples/workers.toml).

Check worker profiles before dispatch:

```bash
orchestrator-engine --project-root /path/to/project worker diagnose --enabled-only
```

Start the wakeup watcher:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host codex --action callback service start --interval-seconds 5
```

Dispatch a task from the host chat and end the turn:

```bash
orchestrator-engine --project-root /path/to/project worker run \
  --worker claude --task-id TASK-001 --prompt-file task-001.md
```

Check health / list pending signals / stop:

```bash
orchestrator-engine --project-root /path/to/project status
orchestrator-engine --project-root /path/to/project doctor
orchestrator-engine --project-root /path/to/project worker tasks --severity warning
orchestrator-engine --project-root /path/to/project watcher \
  --host codex service status
orchestrator-engine --project-root /path/to/project inbox
orchestrator-engine --project-root /path/to/project watcher \
  --host codex service stop
```

Use `status` first for a compact operator report. It summarizes `doctor`,
the active wake channel, worker task diagnostics and verification checks, then
lists only issues and problem tasks/checks that need follow-up.

If a failed historical worker task has been handled manually or superseded by a
successful rerun, keep the task artifacts and add an operator resolution:

```bash
orchestrator-engine --project-root /path/to/project worker resolve \
  --task-id TASK-OLD \
  --status superseded \
  --superseded-by-task-id TASK-NEW \
  --reason "Successful rerun completed the intended work."
```

The resolution lives in `.orchestrator/task-resolutions/`. It stops normal
warning-level status reports from reopening the handled failure, while
`worker tasks --severity info` still shows the historical outcome.

When an adopter project finds an orchestration issue, draft a structured report
instead of pasting huge logs:

```bash
orchestrator-engine --project-root /path/to/project \
  report draft --project-name PROJECT > /tmp/orchestrator-report.md
```

See [docs/operator-reporting.md](docs/operator-reporting.md).
Reports are normally authored by the GitHub account/token that creates the
issue; use `project:*` and `source:*` labels to identify the adopter project
and host chat.

For a Claude host there is no push service; arm a watch from the Claude chat
on:

```bash
orchestrator-engine --project-root /path/to/project watcher stream
```

Manual event emission (for project-side supervisors that run workers
themselves):

```bash
orchestrator-engine --project-root /path/to/project emit \
  --task-id TASK-001 \
  --terminal-status completed \
  --result /path/to/project/result.json \
  --evidence /path/to/project/evidence.json
```

For long checks, use the verification result contract documented in
[docs/contracts.md](docs/contracts.md#verification-result). The portable
reference runner is [examples/check_runner.py](examples/check_runner.py).
Use `orchestrator-engine --project-root /path/to/project checks` to read a
compact status report before opening full logs.

Prune stale notifications, thread-wakeup receipts and rotate the watcher
service log:

```bash
orchestrator-engine --project-root /path/to/project cleanup
```

`cleanup` only removes ephemeral watcher output (notifications,
thread-wakeup receipts, non-current log files) older than
`--retention-days` (default 30) and compacts `watcher-service.log` once it
exceeds `--log-max-bytes`. Terminal events and inbox signals are never
removed by `cleanup`; they are the durable audit trail and are the
responsibility of the adopting project to retire.

## Wakeup contract

The watcher delivers a short deterministic prompt (injected as a Codex turn,
a VS Code chat message, or a JSON stream line for Claude):

```text
LOCAL_AI_ORCHESTRATOR_WAKEUP v1
project: /path/to/project
event_id: ...
task_id: ...
terminal_status: completed
event: ...
evidence: ...
result: ...
requires: ORCHESTRATOR_FOLLOWUP

Read the event/evidence. Verify state and decide the next safe action.
If review is required, inspect the real diff and checks before accepting.
Do not commit or push unless the user explicitly requested it.
```

## Development

```bash
python -m unittest discover -s tests -p 'test_*.py'
ruff check .
```

The test suite includes an install smoke test that creates a temporary virtual
environment, installs the package with `pip install .`, and verifies the CLI,
worker supervisor and stream watcher without `PYTHONPATH`.

Additional documentation:

- [Setup guide (start here)](docs/setup-guide.md)
- [Contracts](docs/contracts.md)
- [Host setup](docs/hosts.md)
- [Project integration and legacy adoption](docs/project-adoption.md)
