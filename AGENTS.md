# Agent instructions

These instructions apply to every AI client working in this repository.

## Scope

This repository provides a portable local orchestration layer for AI workers.
Keep adopter- and provider-specific policy out of the core package. Projects,
hosts and worker providers must connect through explicit adapters and
documented contracts.

## Working rules

- Keep public contracts, documentation, fixtures and examples adopter-neutral.
  Use synthetic project names, identifiers, paths and scenarios unless the user
  explicitly requests a clearly labeled integration guide, compatibility
  profile or case study. Never copy private adopter material into this
  repository. See `CONTRIBUTING.md` for the canonical public-content policy.
- Public documentation and code comments must be written in English.
- Preserve existing user changes.
- Do not commit, push, merge, rebase or perform destructive Git operations
  unless the user explicitly asks.
- Run git stage/commit operations from inside WSL for this checkout. Do not
  stage or commit from Windows Git over `\\wsl.localhost`; it can corrupt
  executable-bit metadata for scripts.
- Prefer deterministic file contracts, atomic writes and reproducible evidence.
- Add or update tests for behavioral changes.
- Do not create or keep an app-level Goal active for work that depends on an
  OrchestratorEngine watcher wakeup. A Goal can keep the current host turn open
  and leave queued completion messages unable to become the next turn. Use a
  durable `workstream`, record `waiting_external` for a named worker/check/CI
  operation, and end the turn so the watcher can resume the chat. Use an
  in-turn deterministic wait only when deliberately choosing not to use the
  watcher delivery path.
- On a workstream continuation wakeup, read `workstream status` before acting.
  Continue only when the descriptor is `active` and the message event ID is
  still the descriptor's `active_continuation.event_id`; otherwise treat the
  queued message as revoked evidence and do not execute its next action.

## Risk-based verification

Before running checks, classify the change and use the narrowest level that
covers its risk:

- **Structural only**: prose documentation, comments, badges or repository
  metadata with no runtime, contract, packaging or generated-output effect.
  Do not run a test suite. Run only relevant structural checks such as parsing
  TOML/JSON, validating links or generated assets, and `git diff --check`.
- **Focused**: an isolated implementation or test change with a clear owning
  module. Run the directly affected tests and lint the touched code. Do not
  upgrade to the full suite merely because it exists.
- **Full**: shared contracts or schemas, CLI behavior, dispatch/watcher/state
  logic, dependencies, build/packaging/CI, cross-module behavior, release
  candidates, or uncertainty left by focused checks. During implementation,
  use focused checks; run the full gate only after the work is otherwise
  complete, immediately before handoff or release. Run:

  ```bash
  python -m unittest discover -s tests -p 'test_*.py'
  ruff check .
  git diff --check
  ```

Do not repeat an already-passing full gate after a later documentation-only
edit unless that edit changes generated artifacts, packaging inputs or test
expectations. For long checks, prefer the detached verification flow in
`docs/verification-policy.md`; on success read only the compact summary, and
open detailed logs only after a failure. If a dependency is unavailable,
report the blocker and run the checks that are available.

Workers and subagents must not monitor tests or other long commands through
repeated status calls, sleeps or log reads. A known short check may use one
foreground blocking tool call. Use `check plan` and `runtime-capabilities` to
select supported foreground or detached execution. When the same implementation
owner must inspect the result and continue debugging, disable wake delivery and
use one bounded wait for that decision phase. Execution duration does not
transfer ownership. A subagent may end after dispatch only through an explicit
handoff that names the operation and enables one terminal wakeup for both
success and failure whenever either outcome requires parent continuation.
`on-failure` is valid only when success needs no parent action. After the
child returns, the parent must record any `waiting_external` state and end its
own active turn so queued delivery can resume it. A pending required check is
not completed work. Never start another AI agent merely to poll the check. A
relay is allowed only as an explicit host fallback when direct parent waiting
and detached wake delivery cannot provide the required bounded bridge. See
`docs/subagent-execution.md`.

Choose exactly one completion route for each operation. For long work, enable
its wake policy and end the turn so the watcher can resume the chat. For a
bounded in-turn wait, dispatch with `--wake-policy never` and use one
`worker wait` or `operation wait`. Do not combine a wake-enabled operation with
a blocking wait unless the user explicitly wants a second queued notification.
In a multi-stage pipeline, intermediate stages use no wakeup; only the terminal
stage that actually hands control back to the chat may emit one.

Before a wake-enabled dispatch that will end the current turn, require a ready
completion channel with `--completion-delivery-mode require-ready`. If admission
reports `not_ready` or `unknown`, keep the turn active, repair or re-arm the
documented channel, and retry the dispatch. `ready` is only point-in-time
evidence, not a future-delivery guarantee. Use `warn` only while an agent or
operator remains present to act on the warning; use `off` only when another
explicit completion route owns continuation.

If a final full gate fails, inspect the failed check, fix with focused tests,
and run the full gate again only when a new final candidate is ready. Do not
run the complete suite after every intermediate edit.

## Accepted-plan execution

New workstreams have no continuation-count or total wall-time ceiling unless
the owner explicitly chooses limits. Keep old explicit limits until an
authorized policy update; never edit live descriptors by hand. There is no
daily slice quota or mandatory task token budget.

An integration package includes all dependent slices needed for one finished
user outcome or contract. Complete the package and review the combined diff
before its full gate. A worker slice alone does not establish that readiness.
Use focused checks during implementation, including completed critical
foundations; after a failed full gate, return to focused fixes until the next
finished candidate.

Independent tasks may run concurrently. Serialize operations that mutate the
same database, document or overlapping files; use isolated worktrees and
existing project/resource locks where appropriate. Do not impose a global
worker count merely to serialize one shared resource. Quota exhaustion preserves
unfinished work and its next action; use non-AI availability monitoring with
backoff and one terminal wakeup instead of repeated model calls.
