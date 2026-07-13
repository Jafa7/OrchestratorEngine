# OrchestratorEngine

[![CI](https://github.com/Jafa7/OrchestratorEngine/actions/workflows/ci.yml/badge.svg)](https://github.com/Jafa7/OrchestratorEngine/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Lint: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Release](https://img.shields.io/github/v/release/Jafa7/OrchestratorEngine?label=release&color=informational)](https://github.com/Jafa7/OrchestratorEngine/releases/latest)

Local event-driven coordination for AI agents and detached CLI workers:
durable results and evidence, compact status checks, and host-specific
completion delivery without engine-managed provider API keys.

OrchestratorEngine is a small event-driven coordination layer for AI worker
processes. A user orchestrates from a host chat (Claude Code / Claude for
Windows, VS Code Copilot, or Codex Desktop with the limitation documented
below), dispatches tasks to CLI workers, and ends the turn. Workers run
detached, write a terminal event to disk when they finish, and a local watcher
routes the completion through the dispatching host's configured delivery
channel — without engine-managed provider API keys or token-spending heartbeat
prompts. Host and worker CLIs retain responsibility for their own local
authentication.

Supported host/worker combinations are symmetric: any host chat can manage any
CLI workers (Claude, Codex, Copilot, or any other command-line worker).
Long verification runs can use the same flow: run checks detached, keep full
logs as artifacts, and return a compact pass/fail summary through that channel.

Host delivery quality is provider-specific. Claude stream wakeups are the
recommended live orchestration path today: the already-open Claude chat wakes
and continues in that session. VS Code uses its chat CLI. Codex Desktop on
Windows can receive durable delivery into thread history and best-effort
window focus/refresh, but it does not currently provide a reliable live wakeup
channel for the already-open Desktop agent. Codex remains fully supported as a
CLI worker through `codex exec`.

## Measured coordination context reduction

The graph below shows one practical benefit even when a host such as Codex
Desktop cannot wake live: status checks can read compact task state instead of
repeatedly loading growing worker logs. Lower is better.

![Context read while checking background work](docs/assets/coordination-context.svg)

| Scenario | Full-log polling | Status reads | Context read | Reduction |
| --- | ---: | ---: | ---: | ---: |
| Long test | 655.4 KB | 17.9 KB | 2.73% | 97.27% |
| AI worker | 2.50 MB | 17.9 KB | 0.68% | 99.32% |
| Three parallel workers | 3.75 MB | 20.5 KB | 0.52% | 99.48% |

This is selective inspection, not output truncation. The status report keeps
task states, diagnostics, log sizes and paths compact; complete stdout,
stderr, result and evidence artifacts remain available for targeted or full
reading when needed.

The measurement uses four checks against deterministic growing logs and UTF-8
bytes as a provider-neutral proxy for context volume. It does not claim the
same percentage of total token or engineering cost for every workflow. Codex
agents can avoid those intermediate model calls by handing `worker wait` to
the user's terminal; Claude live wakeup avoids the manual return step as well.
See the reproducible
[measurement methodology](docs/coordination-efficiency.md).

## Connecting the engine to your project

**If you are an AI agent** asked to set this up: follow
[docs/setup-guide.md](docs/setup-guide.md) step by step. It contains every
command, a check after each step, and a troubleshooting table. Do not improvise
a different layout — the file contract is what makes deterministic delivery
work.

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
- Route a bounded pointer to event/evidence/result through the bound host
  channel.
- Avoid token-spending heartbeat prompts.
- Keep provider integrations at explicit adapter boundaries.
- Provide service-style watcher control: start, status, stop and restart.

## Non-goals

- This is not an AI agent runtime.
- This does not own product-specific task contracts.
- This does not replace Codex, Claude, Copilot or project-local review logic.
- This does not manage provider API keys or call provider APIs directly; local
  host and worker CLIs own their authentication.

## How it fits together

1. **Bind** the project to the host chat once
   (`bind --host codex|claude|vscode`).
2. **Dispatch** tasks from the host chat (`worker run`), which returns
   immediately; a detached supervisor runs the worker CLI and emits a terminal
   event on exit.
3. **Deliver**: a watcher service (`--action callback`) sends a follow-up to
   VS Code, while Claude watches `watcher stream`. Codex App Server turns are
   history-only and do not refresh the already-open Desktop chat. Callback
   services can be scoped with `watcher --host vscode` so
   multiple host channels can share one inbox without consuming each other's
   signals.

Per-host setup details: [docs/hosts.md](docs/hosts.md).

Release and upgrade notes:
[CHANGELOG.md](CHANGELOG.md), [LICENSE](LICENSE), and
[docs/upgrade-guide.md](docs/upgrade-guide.md).

## File layout inside an adopted project

By default the orchestrator and adopting agents use `.orchestrator/` in the
target project:

```text
.orchestrator/
  workers.toml
  policies/
    quality-efficient.md
  prompts/
    <prompt>.md
  task-resolutions/
    <task_id>.json
  events/
    <event_id>.json
  tasks/
    <task_id>/
      task.json
      effective-prompt.md
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
    acknowledgements/
      <host>/
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
orchestrator-engine --project-root /path/to/project adopt --host vscode
```

Bind the project to the host chat (this quick start uses VS Code because its
callback path can be exercised end to end):

```bash
orchestrator-engine --project-root /path/to/project bind --host vscode
```

Configure workers in `/path/to/project/.orchestrator/workers.toml`:

```toml
[policies.quality-efficient]
files = ["policies/quality-efficient.md"]
quality_priority = "correctness-first"
context_strategy = "progressive"
verification_strategy = "risk-based-final-gate"
output_strategy = "compact-evidence"

[workers.claude]
enabled = true
command = ["claude", "-p", "--model", "sonnet", "--effort", "high",
           "--dangerously-skip-permissions"]
prompt_via = "stdin"
policy = "quality-efficient"
expect_long_running = true
permission_profile = "full"

[workers.codex]
enabled = true
command = ["codex", "exec", "--json", "-m", "gpt-5.6-terra",
           "-c", "model_reasoning_effort=\"high\"",
           "-c", "approval_policy=\"never\"",
           "-c", "sandbox_mode=\"danger-full-access\""]
prompt_via = "arg"
policy = "quality-efficient"
expect_long_running = true
permission_profile = "full"

[workers.copilot]
enabled = true
command = ["copilot", "--model", "auto", "--effort", "high",
           "--allow-all", "--no-ask-user", "--prompt"]
prompt_via = "arg"
policy = "quality-efficient"
expect_long_running = true
permission_profile = "full"
```

These three profiles are fully autonomous and should be enabled only for a
trusted project. For fast/default/deep plus restricted and read-only examples,
start from [examples/workers.toml](examples/workers.toml), then enable only the
profiles supported by the installed CLIs and account.

`adopt` creates the referenced correctness-first policy. Existing adopters can
copy [examples/policies/quality-efficient.md](examples/policies/quality-efficient.md).
The engine snapshots policy + task bytes before dispatch and records their
hashes in task/evidence, so workers consistently use progressive context,
risk-based verification and compact handoffs without an arbitrary token cap.
See [worker behavior policies](docs/worker-policies.md) for the behavioral
model, role overlays, audit contract and limitations.

Check worker profiles before dispatch:

```bash
orchestrator-engine --project-root /path/to/project worker diagnose --enabled-only
```

An adopter may configure a bounded, non-AI `availability_probe` for a profile.
Run it explicitly before dispatch when local quota or account tooling can
provide a deterministic answer:

```bash
orchestrator-engine --project-root /path/to/project worker availability \
  --worker codex
orchestrator-engine --project-root /path/to/project worker run \
  --worker codex --task-id PREFLIGHT-001 --prompt-file task-001.md \
  --availability-mode require-available
```

`block-unavailable` preserves the legacy advisory behavior;
`require-available` fails closed unless the adopter-owned probe returns
`available`. Checked dispatches record only bounded status/hash metadata. The
engine does not invent a provider quota API or spend model tokens polling.

Start the VS Code delivery watcher (use `watcher stream` from Claude; for Codex,
review durable history manually):

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host vscode --action callback service start --interval-seconds 5
```

Dispatch a task from the host chat and end the turn:

```bash
orchestrator-engine --project-root /path/to/project worker run \
  --worker claude --task-id TASK-001 --prompt-file task-001.md
```

When Codex Desktop cannot refresh the open chat, leave the user one compact
terminal monitor instead of polling from the model:

```bash
orchestrator-engine --project-root /path/to/project worker wait \
  --task-id TASK-001
```

The command updates one colored line without AI calls, rings the terminal bell
when supported and tells the user when to return to the chat. Add `--json` for
one bounded machine-readable result with no live display. It stops with a red
`ACTION` message instead of waiting forever when the supervisor is dead, its
heartbeat is stale or terminal result state is unreadable.

Optional concurrency, intent and recovery controls stay deterministic and
local. Limits live in `workers.toml`; operator actions are explicit:

```bash
orchestrator-engine --project-root /path/to/project worker queue tick
orchestrator-engine --project-root /path/to/project worker cancel \
  --task-id TASK-001 --mode graceful --reason "superseded"
orchestrator-engine --project-root /path/to/project worker retry \
  --task-id TASK-001 --max-attempts 3 --reason "provider quota reset"
orchestrator-engine --project-root /path/to/project status --since CURSOR
```

Exact active duplicates are blocked by default. Structured worker handoffs and
usage telemetry are optional evidence; neither can instruct core control flow.
Complete file deliverables belong below the task-local declared `outputs/`
directory and are hashed into `worker-outputs.json`; provider-owned plan/cache
files are deliberately not treated as durable results.

Check health / list pending signals / stop:

```bash
orchestrator-engine --project-root /path/to/project status
orchestrator-engine --project-root /path/to/project doctor
orchestrator-engine --project-root /path/to/project worker tasks --severity warning
orchestrator-engine --project-root /path/to/project watcher \
  --host vscode service status
orchestrator-engine --project-root /path/to/project inbox
orchestrator-engine --project-root /path/to/project watcher \
  --host vscode service stop
```

Use `status` first for a compact operator report. It summarizes `doctor`,
the active delivery channel, worker task diagnostics and verification checks,
then lists only issues and problem tasks/checks that need follow-up.

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

`worker diagnose` also compares the bundled `quality-efficient` policy hash
with the selected project-local copy. It reports differences for explicit
review but never overwrites adopter policy.

For AI review, implementation, verification and adopter-report workers, start
from the reusable prompt templates in [examples/prompts](examples/prompts).
They keep worker output compact: summaries and artifact paths first, full logs
only as durable files, and small excerpts only when a failure needs context.
`worker tasks` also reports `task_large_worker_log` when stdout/stderr or the
supervisor log is large enough that a host chat should avoid reading it whole.

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

## Follow-up message contract

A terminal event produces a short deterministic follow-up message. Depending
on the bound host, it is submitted to a headless Codex App Server and stored
in thread history, sent to VS Code chat, or emitted as a JSON stream line for
Claude. Codex Desktop history delivery does not refresh or wake an already-open
Desktop chat.

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

Use the [risk-based verification policy](docs/verification-policy.md): prose
and metadata-only edits get structural checks, isolated behavior gets focused
tests, and shared contracts, packaging or release candidates get the full gate.
Do not repeat a passing full gate after a later prose-only edit.

```bash
python -m pip install '.[test]'
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

## License

OrchestratorEngine is available under the permissive [MIT License](LICENSE).
Copyright remains with Oleg Synelnykov (Jafa7); copies or substantial portions
must retain the copyright and license notice.
