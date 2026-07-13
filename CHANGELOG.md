# Changelog

All notable changes to OrchestratorEngine are documented here.

## [Unreleased]

## [0.5.0] - 2026-07-13

### Added

- `upgrade check` provides a bounded, read-only adopter readiness report for
  engine/schema health, enabled worker profiles, dispatch settings, local
  policy drift and required manual audits.
- `worker policy export` exposes the installed bundled policy for explicit
  comparison without silently overwriting adopter customizations.

### Changed

- Worker policy revision 2 makes task intent verification authoritative over
  generic or copied task prose, and strict AI profile diagnostics flag missing
  admission verification declarations.
- Setup and upgrade guidance now require an explicit verification intent and
  provide an agent-ready adopter upgrade checklist.
- README onboarding now uses one concise Quick start, while the canonical
  setup guide provides a release-first, strict-compatible Step 0–8 procedure.

## [0.4.1] - 2026-07-13

### Fixed

- Aggregate worker wait excludes unhealthy/action-required tasks from
  `active_count` and validates direct group snapshot calls consistently.
- CI no longer repeats the complete branch gate when a release tag is pushed.
- The wait JSON documentation distinguishes single-task and group status
  objects without an ambiguous lead sentence.

## [0.4.0] - 2026-07-13

### Added

- `worker wait` accepts repeated task ids with deterministic `any` and `all`
  aggregate modes, bounded group JSON, compact TTY status and preserved
  single-task compatibility.

### Documentation

- Documented the verified Codex in-turn continuation path, including direct
  deterministic waits, the limited relay-subagent role, token tradeoffs,
  failure recovery and the boundary from detached live wakeup.

## [0.3.3] - 2026-07-13

### Added

- Hash-bound artifact resolutions provide a non-destructive lifecycle for
  reviewed historical malformed schema metadata while preserving every
  original byte and all prior companion records.

### Fixed

- Superseded tasks can retain diagnostic-scoped resolutions, so stale
  historical profile warnings no longer affect aggregate health without
  discarding the successful replacement relationship.
- The coordination benchmark now pins a synthetic engine identity and keeps
  its machine-readable result, SVG and documentation tables synchronized.
- Artifact resolution reads reject symlink swaps and concurrent file changes;
  immutable companions use exclusive creation, and list paths round-trip into
  the resolve command without path rewriting.
- Schema diagnostics no longer double-report invalid resolution companions or
  expose a nonzero actionable unsupported count after a finding is resolved.

## [0.3.2] - 2026-07-13

### Fixed

- Generated worker prompts now include a complete schema-valid optional
  `WORKER_HANDOFF` example, and runtime validation enforces its bounded array
  shapes consistently with the public schema.
- Completed tasks can durably acknowledge specific non-error diagnostics after
  operator verification. Matching warnings remain visible as information,
  while error diagnostics can never be downgraded.

## [0.3.1] - 2026-07-13

### Added

- Read-only bundled worker-policy revision and hash diagnostics identify when
  a project-local `quality-efficient` policy differs without overwriting
  intentional adopter customizations.
- A deterministic release consistency checker validates package, source, lock,
  changelog and installation-document version markers.

### Changed

- CI and install smoke derive the expected wheel version from checked release
  metadata instead of maintaining another hard-coded version string.

## [0.3.0] - 2026-07-13

### Added

- `worker wait` provides a compact color-aware blocking terminal monitor that
  performs no model polling and tells Codex Desktop users when to return to the
  chat for result review. It reports dead/stale supervisors and incomplete
  terminal state as operator action instead of waiting indefinitely.
- Opt-in dispatch admission modes add strict adopter-owned availability checks
  and full task-intent/profile compatibility declarations while preserving
  legacy advisory preflight and permission-only enforcement behavior.

### Changed

- The quality-efficient worker policy keeps implementation ownership through
  risk-selected final verification, uses deterministic blocking check runners
  instead of model polling, and reserves low-cost AI analysis for failures
  where bounded evidence needs genuine diagnosis.

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
