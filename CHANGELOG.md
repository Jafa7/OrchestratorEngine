# Changelog

All notable changes to OrchestratorEngine are documented here.

## [Unreleased]

### Added

- Machine-readable host delivery capabilities in status, wake receipts, and
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
- Opt-in Codex GPT-5.6 worker profiles map Luna, Terra and Sol to fast,
  default and deep orchestration tiers while retaining GPT-5.5 fallbacks.
- Audit-preserving, host-scoped manual inbox acknowledgement receipts, with
  explicit single-event and confirmed bulk modes.
- Explicit bounded worker availability probes and narrow rate-limit result
  classification.

### Changed

- Codex Desktop wake receipts now clearly distinguish a completed headless App
  Server turn from a refresh of the open Desktop chat.
- CI now installs the test extra, validates package schemas, bounds jobs and
  checks clean checkout whitespace/diff state.

## [0.1.0] - 2026-07-08

### Added

- Stable v0.1 file contracts for terminal events, inbox signals, bindings,
  wake targets, watcher state, worker tasks and verification results.
- Detached worker dispatch with durable stdout/stderr/result/evidence
  artifacts.
- Per-task `wake_target` snapshots so multi-chat dispatch wakes the chat that
  launched each task.
- Callback wakeups for Codex and VS Code hosts, plus stream wakeups for Claude
  hosts.
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
