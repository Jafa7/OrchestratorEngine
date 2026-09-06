# Changelog

All notable changes to OrchestratorEngine are documented here.

## [Unreleased]

## [1.4.1] - 2026-09-06

### Fixed

- Classified Codex Doctor's exact `terminal.env` finding as a bounded
  noninteractive-probe limitation while preserving the provider status and
  keeping every other warning or failure actionable.

## [1.4.0] - 2026-09-06

### Added

- Added complete, partial, unavailable and unverified usage measurement states;
  the Codex-specific adapter accepts only final `turn.completed.usage` records
  instead of unrelated JSON token fields.
- Added verification handoff evidence and task acceptance diagnostics separate
  from successful worker process completion.

### Changed

- Verification intent is now an escalation-safe baseline: concrete discovered
  risk may raise the level when the broader check is safe and authorized.
- Documented a matched-task evaluation method for measuring total token use,
  missed defects and rework instead of treating context bytes as quality proof.

### Fixed

- Codex host diagnostics now terminate and verify the launcher process tree on
  timeout instead of leaving inherited child processes running.
- Malformed or internally inconsistent `codex doctor --json` reports now fail
  closed and cannot produce a successful CLI exit code.

## [1.3.0] - 2026-09-06

### Added

- Added an explicit `codex diagnose` adapter command that runs bounded
  `codex doctor --json`, prefers the project binding launcher, and returns only
  a privacy-safe machine-readable summary.
- Published Codex worker examples use `codex exec --ephemeral`, and worker
  diagnostics identify profiles that would otherwise retain provider sessions.
- Added an opt-in `review-efficient` worker-policy overlay with changed-surface
  discovery, findings-first handoff, and example Codex usage telemetry budgets.

### Changed

- Codex binding guidance now treats legacy rollout discovery as a best-effort
  compatibility heuristic and documents explicit recovery for migrated
  Desktop threads and watcher restart after host reboot.

## [1.2.0] - 2026-09-06

### Added

- Added `worker run --wake-policy always|on-failure|never` and bounded
  operation-wait diagnostics for targets that also have host wakeups enabled.

### Changed

- Documented mutually exclusive completion routes: either end the turn and
  use watcher delivery, or disable wakeup and use one blocking local wait.
- Reframed the README around the intended user, coordination boundary and
  token-efficient completion choices before implementation detail.

## [1.1.0] - 2026-09-06

### Fixed

- Made detached local checks own and stop active command process groups before
  reaping, and reject unsupported descriptor schemas on mutation.
- Made Claude stream delivery retryable when writing to the host channel fails;
  successful delivery is deduplicated by stable event ID.
- Classified unavailable Linux process identity as `unknown` instead of
  confirmed process exit, and made identity-safe waits fail closed.
- Watcher scans now advance pending worker queue entries, including delayed
  retries after their deterministic `not_before` timestamp becomes due.
- Made workstream transitions reconcile before mutation, kept completed streams
  terminal, migrated legacy continuation authorization once, separated
  checkpoint and generated-artifact namespaces, and isolated reconciliation
  failures per workstream. Schema surveys include the new artifact directories.
- `watcher service restart` now inherits an existing service's action,
  interval, state path and legacy target when those options are omitted, and
  holds one lifecycle lock across stop and start.

## [1.0.1] - 2026-09-05

### Fixed

- Worker tasks now execute the hash-bound normalized profile captured at
  dispatch, so later `workers.toml` edits cannot change queued or starting
  commands and permission flags.
- Local checks, GitHub Actions monitors and pull-request monitors atomically
  claim their shared verification-result directory and reject cross-type ID
  collisions before an earlier result can be overwritten.
- Codex and VS Code wakeup delivery plus watcher service lifecycle operations
  are serialized across processes; ambiguous Codex queue outcomes require an
  explicit retry and cannot be duplicated by a concurrent consumer.
- Worker terminal descriptors are published before dispatch claims and leases
  are released, and the reaper reconciles the historical released-lease crash
  window from existing terminal artifacts.
- Multi-project watcher state now scopes delivery by resolved project root and
  event ID while retaining legacy unscoped summary fields and conservative
  migration behavior.
- Workstream checkpoints now recover interrupted descriptor/event publication,
  use unambiguous operation identities, revoke superseded timer continuations,
  and enforce wall-time at delivery. The continuation budget also covers
  `waiting_external` resumptions instead of timer signals alone.

## [1.0.0] - 2026-09-05

### Changed

- Graduated the verified `1.0.0rc1` runtime and public contracts to the first
  stable `1.x` release without additional runtime changes.
- Marked the package as production/stable and updated canonical installation
  guidance to the immutable `v1.0.0` tag.

## [1.0.0rc1] - 2026-09-05

### Added

- The public `1.x` compatibility policy now defines durable schema, CLI,
  configuration, platform and host-adapter guarantees together with the
  verified historical upgrade floor.
- A public security policy documents executable project configuration,
  untrusted worker output, credential ownership, audit retention and private
  vulnerability reporting boundaries.
- Release automation supports immutable `rcN` tags and publishes them as
  GitHub prereleases without changing the latest stable release.
- Historical upgrade verification accepts stable and `rcN` installed CLI
  versions, so the same read-only fixture gate covers release candidates.

### Security

- Event identifiers are now constrained to a bounded path-safe alphabet before
  they are used in event, signal or receipt paths.
- Watcher services record process identity and refuse to signal a live PID when
  that identity is absent or no longer matches, preventing PID-reuse shutdowns
  from targeting an unrelated process.
- Watcher startup terminates its owned child if identity capture or durable
  service-state publication fails, including kill escalation after a bounded
  termination timeout.
- Watcher stop rejects unsupported future service-state schemas before
  mutating the file or signalling a recorded process.

## [0.13.0] - 2026-09-05

### Added

- CI now runs a bounded 20-iteration installed-wheel conformance soak and a
  SHA-256-pinned historical upgrade matrix for releases 0.10.0 through 0.12.0.
- Maintainer tools produce bounded reliability and upgrade-path reports while
  preserving failed synthetic fixtures and durable audit artifacts.
- Version 1 readiness and the supported historical compatibility floor are
  documented without expanding the provider-neutral runtime scope.

## [0.12.0] - 2026-09-05

### Added

- Release source provenance now has a deterministic helper and temporary-Git
  regression matrix covering rewritten local tag refs, lightweight remote
  tags, event-SHA mismatch and release commits outside the main branch.
- `operation wait` can block once on up to 64 mixed worker, local-check, CI and
  PR descriptors with `any`/`all` semantics, compact terminal output and no
  model calls, provider queries or log reads.
- `operation status` exposes the same bounded mixed-operation snapshot without
  blocking, for scripts, dashboards and operator diagnostics.

### Fixed

- Worker and heterogeneous waits no longer misclassify the normal
  result-to-descriptor finalization window as a dead supervisor when the
  supervisor exits between two bounded state reads; genuinely stalled
  finalization remains detectable through the stale-heartbeat threshold.

## [0.11.1] - 2026-09-05

### Fixed

- Release validation now fetches the remote annotated tag object into a
  dedicated internal ref before inspecting it, avoiding the lightweight local
  tag ref produced by tag-event checkout while preserving immutable tag and
  exact-SHA provenance checks.
- Release source validation now reports the failed tag, event-SHA or main-line
  invariant explicitly instead of exiting from a silent shell assertion.

## [0.11.0] - 2026-09-05

### Added

- Confirmed GitHub Actions failures now receive one bounded jobs query and
  retain compact problem job/step metadata without downloading CI logs or
  changing the authoritative monitor result when diagnostics are unavailable.
- A tag-triggered release workflow verifies exact-SHA branch CI, builds and
  smoke-tests wheel/sdist artifacts, publishes through a draft boundary and
  checks GitHub asset digests before making the release public.
- Deterministic release helpers validate CI provenance, extract one versioned
  changelog section and generate `SHA256SUMS`; focused tests cover stale runs,
  failed reruns, tag/version mismatches and remote asset drift.

### Fixed

- Watcher shutdown now joins its heartbeat thread before returning, preventing
  late atomic heartbeat writes from racing temporary-directory cleanup.
- Install smoke failures retain bounded nested CLI stdout/stderr tails so a
  conformance failure remains diagnosable from the failed CI step.

## [0.10.0] - 2026-09-05

### Added

- GitHub Actions monitors can now start before a run ID is visible, discover
  one exact allowlisted run from a full commit SHA and optional workflow name,
  then reuse the existing detached exact-run observation and wakeup lifecycle.
- SHA discovery filters bounded `gh run list` metadata locally, fails closed on
  multiple matches or duplicate active run ownership, and preserves compact
  discovery evidence without storing provider output.
- A provider-free `conformance run` command verifies a clean installation
  through an isolated portable or full detached synthetic-worker fixture,
  bounded machine-readable evidence and idempotent watcher delivery.
- Clean-fixture conformance validates the generated layout, disabled example
  profile, bundled worker policy, conservative dispatch defaults and
  create-only adoption idempotency before exercising runtime artifacts.
- Create-only adoption writes generated worker and policy templates with
  canonical LF line endings on every platform, keeping bundled-policy hashes
  stable on native Windows.
- The packaged `conformance-report` schema makes that self-test result a
  versioned public contract; successful fixtures are removed by default while
  failed fixtures are retained for diagnosis.
- A provider-free crash-recovery matrix verifies convergence at interrupted
  result, evidence, event, signal, notification and watcher-state boundaries
  without mutating adopter state.
- Full conformance runs six concurrent synthetic workers, exercises aggregate
  wait-any/wait-all behavior and verifies host-scoped signal partitioning from
  dispatch-time wake-target snapshots.
- Full conformance also verifies one-shot recovery of an expired unclaimed task
  descriptor through the worker reaper, including durable failure evidence and
  idempotent host-scoped signal consumption.
- CI runs portable conformance from installed Windows and macOS packages and
  full conformance from the Linux wheel without `PYTHONPATH`; the report schema
  requires every recovery scenario and the exact host-routing partition.

## [0.9.0] - 2026-09-05

### Added

- A machine-readable `runtime-capabilities` report, portable advisory file
  locking and Windows/macOS CI smoke coverage make the supported platform
  boundary explicit.
- Native Windows and macOS can import and inspect the portable core and run
  compatible foreground checks; detached lifecycle commands fail before
  creating runtime artifacts and direct adopters to Linux or WSL.

### Fixed

- GitHub Actions and pull-request monitors now pin the requested GitHub host,
  compare repository ownership case-insensitively, reject non-finite polling
  values and validate the repository identity returned by `gh`.
- Install smoke waits for terminal monitor and check descriptors instead of
  earlier evidence files, closing publication races in CI and PR scenarios.
- Native Windows process inspection uses a non-signalling process handle
  query instead of `os.kill(pid, 0)`, preventing a read-only status command
  from emitting a Windows console control event or terminating a process.
- Worker, local-check, CI and PR reapers now fail closed when Linux process
  identity is unavailable; identity checks return `unknown` rather than
  treating an inaccessible Linux `/proc` record as proof of process exit.
- The platform capability check is sequenced after installing the release that
  provides it, keeping the canonical setup flow executable end to end.

## [0.8.1] - 2026-09-05

### Fixed

- The detached local-check integration test now waits for the terminal check
  descriptor rather than the earlier result artifact, removing a Python 3.11
  CI race without changing runtime contracts.

## [0.8.0] - 2026-09-05

### Added

- Detached exact-PR readiness monitoring through the adopter-authenticated
  GitHub CLI, pinned to an immutable expected head SHA with optional approval
  enforcement, bounded evidence, explicit terminal classifications and the
  existing dispatch-time chat wake target.
- `pr watch/status/cancel/retry/reap` lifecycle commands and aggregate status
  visibility for ready, changed-head, failed-check, review, conflict, transport
  and supervisor-recovery outcomes.

## [0.7.0] - 2026-09-05

### Added

- Detached exact-run GitHub Actions monitoring through the locally
  authenticated `gh` CLI, with repository allowlisting, optional run-attempt
  and commit-SHA checks, bounded evidence, compact `checks` integration and
  dispatch-time host wakeup.
- Provider-neutral `ORCHESTRATOR_TERMINAL` and
  `ORCHESTRATOR_FOLLOWUP_SIGNAL` contracts let deterministic external
  operations reuse the existing watcher without pretending to be AI workers.
- CI wake policies can deliver every terminal result or remain quiet on a
  successful run while preserving its durable result and audit event.
- Identity-safe recovery for crashed GitHub Actions monitors through `ci reap`,
  plus bounded-at-read `gh run view` capture and machine-readable launch and
  cancellation records.
- Explicit bounded workstream checkpoints with delayed, idempotent continuation
  wakeups and fail-closed `needs_user`, `blocked`, `paused` and completion
  states. Ending a chat turn alone never authorizes continuation.
- A documented external-tool matrix makes optional `gh`, Codex, Claude,
  Copilot and VS Code CLI prerequisites explicit without letting core install
  tools or manage credentials.
- First-class local `check plan/run/status/reap` commands choose foreground or
  detached execution from configured estimates or bounded successful-run
  history, using a 30-second default threshold and detached wakeups without
  model polling.
- Detached local checks persist their supervisor identity before returning;
  aggregate status exposes crashed/stalled runtimes and `check reap` recovers
  durable terminal artifacts or records an explicit error without rerunning.
- Local check output stays complete in durable logs; successful JSON evidence
  records hashes and sizes without copying output tails, while failures retain
  only bounded diagnostic excerpts.

### Security

- GitHub monitoring uses argv execution without a shell, never requests or
  persists authentication tokens, redacts common token forms, bounds captured
  output and fails closed for repositories outside adopter-owned configuration.

## [0.6.0] - 2026-09-05

### Added

- Codex Desktop live task delivery through the local `codex queue` command,
  using each task's dispatch-time target snapshot and a bounded deterministic
  wakeup message.
- Queue receipts record `status: "queued"`, the acknowledged message id and
  the actual delivery capability without claiming that the agent turn already
  completed.

### Changed

- Codex callback services prefer the shared live session queue and retain the
  headless App Server turn as an automatic compatibility fallback for older
  CLIs.
- Host capabilities and setup documentation distinguish live queue acceptance,
  headless history completion and optional window focus.

### Fixed

- Ambiguous queue outcomes require manual review immediately instead of blind
  automatic retry, preventing duplicate live wakeup messages when acceptance
  cannot be proven.
- Codex callbacks recover from stale versioned Windows launcher paths after a
  Desktop app update while preserving the original task wake-target snapshot.

## [0.5.1] - 2026-07-15

### Documentation

- Added a canonical contributor policy for adopter-neutral public contracts,
  fixtures, examples and documentation, with explicit boundaries for approved
  integration guides, compatibility profiles and case studies.
- Added a portable agent-instruction rule and upgrade audit so adopting
  projects can apply the same privacy-safe synthetic-example policy.
- Replaced remaining adopter-specific reporting examples with synthetic
  project labels and included `CONTRIBUTING.md` in source distributions.

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
