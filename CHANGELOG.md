# Changelog

All notable changes to OrchestratorEngine are documented here.

## [Unreleased]

## [0.2.0] - 2026-07-12

### Added

- Reproducible coordination-context benchmark and README chart compare compact
  status polling with repeated cumulative-log reads, including an explicit
  Codex Desktop interpretation and quality guard.
- Portable risk-based verification policy defines structural, focused and full
  gates for host agents, detached workers and adopting projects.
- Provider-neutral worker behavior policies can be selected per profile,
  composed into immutable dispatch-time prompts and audited through packaged
  policy snapshot schemas and prompt/file hashes.

### Changed

- Task descriptors have a single writer: `worker run` writes `task.json` before
  the spawn and hands it over, and the supervisor claims it with its own
  `supervisor_pid` as its first action. A dispatched task therefore reports
  `starting` until its supervisor claims it, and a fast worker's terminal
  descriptor can no longer be overwritten by the dispatcher.
- Workers run in their own process group (`worker_pgid` on the descriptor), and
  a timed-out worker is stopped group-wide — `SIGTERM`, bounded grace, then
  `SIGKILL` — so its subprocesses cannot outlive the task. `result.json` records
  the signal ledger in an optional `termination` object.
- Supervisors now hold a durable Linux process-identity lease. `worker reap`
  safely finalizes tasks whose supervisor is proven gone, emitting one
  deterministic terminal event without signaling reused PIDs or deleting
  audit artifacts.
- Added bounded global/per-profile admission, a durable FIFO queue, graceful or
  forced task cancellation, exact active-dispatch duplicate protection,
  structured task intent and bounded retry lineage.
- Added opaque delta-status cursors, mechanical progress diagnostics, optional
  JSON-lines usage telemetry, advisory soft budgets and bounded structured
  worker handoff evidence.
- Added provider-neutral task-local declared outputs with bounded hashing and a
  Claude plan-mode diagnostic, preventing a provider-owned plan file from being
  mistaken for the durable primary result.
- Aggregate status large-log summaries now expose the corresponding artifact
  paths so agents can drill down without loading full logs by default.
- README badges, package metadata and repository positioning now describe
  host-specific delivery without promising universal live wakeup or zero
  polling.

## [0.1.1] - 2026-07-11

### Added

- Machine-readable host delivery capabilities in status, delivery receipts, and
  the read-only `host-capabilities` report.
- Draft 2020-12 schemas, conformance fixtures and a read-only `schemas` CLI
  for the stable v0.1 durable artifacts.
- Read-only `status` aggregates doctor, wake channel, worker task and
  verification check summaries into one compact operator report.
- `report draft` creates a Markdown GitHub issue draft from the compact
  status report.
- GitHub issue templates and operator reporting docs standardize
  adopter-project problem reports.
- Project/source label conventions identify report origin independently of the
  GitHub account that created the issue.
- Operator task resolutions (`worker resolve`, `worker resolutions`) let
  historical failed tasks be marked `acknowledged` or `superseded` without
  deleting or rewriting durable audit artifacts.
- Worker output economy guidance, prompt templates and large-log diagnostics
  help agents read compact artifacts before spending tokens on full logs.
- Codex GPT-5.6 worker profiles map Luna, Terra and Sol to fast, balanced and
  quality-first orchestration tiers.
- Audit-preserving, host-scoped manual inbox acknowledgement receipts, with
  explicit single-event and confirmed bulk modes.
- Explicit bounded worker availability probes and narrow rate-limit result
  classification.

### Changed

- Codex Desktop delivery receipts now clearly distinguish a completed headless
  App Server turn from a refresh of the open Desktop chat.
- Worker diagnostics recognize the official full-access automation flags for
  detached Codex and Claude profiles.
- Public setup, host, reporting and worker-profile documentation now uses
  capability-accurate delivery language and privacy-safe report guidance.
- CI now installs the test extra, validates package schemas, bounds jobs and
  checks clean checkout whitespace/diff state.

## [0.1.0] - 2026-07-08

### Added

- Stable v0.1 file contracts for terminal events, inbox signals, bindings,
  wake targets, watcher state, worker tasks and verification results.
- Detached worker dispatch with durable stdout/stderr/result/evidence
  artifacts.
- Per-task `wake_target` snapshots so multi-chat dispatch routes completion to
  the host target that launched each task.
- Callback history delivery for Codex, callback UI delivery for VS Code, and
  live stream wakeups for Claude hosts.
- Watcher service control, heartbeat/status diagnostics and stale/crashed
  service warnings.
- Deferred callback state with bounded retries, manual-required quota
  handling and explicit acknowledgement.
- Reference verification runner and worker profile examples.
- Read-only `worker diagnose` reports advisory profile diagnostics with
  deterministic severities and automation-friendly exit codes.
- Read-only `worker tasks` reports runtime diagnostics for detached task
  artifacts, stale heartbeats and missing results/evidence.
- Read-only `checks` reports compact verification status, summary paths and
  failed command logs for `.orchestrator/checks` artifacts.
- `watcher service status` warns when a bare legacy status view differs from
  the bound host-scoped callback channel.
- Install smoke coverage that exercises the installed CLI without
  `PYTHONPATH`.

### Documented

- Host live-wakeup limits, including Codex Desktop Windows durable delivery
  versus true live wakeup.
- Non-interactive worker profile guidance for Codex, Claude and Copilot.
- Setup guide for adopting OrchestratorEngine in a clean project.

### Notes

- OrchestratorEngine is provider-neutral core infrastructure. Project-specific
  adapters, private paths and retention policies belong in adopting projects.
