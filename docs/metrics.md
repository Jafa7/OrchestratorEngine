# Productivity metrics and process advisor

OrchestratorEngine provides an optional local metrics subsystem for answering
workflow questions from durable evidence. It does not run model calls, change
worker selection, lower verification, infer subscription cost or become a new
acceptance authority.

Metrics are disabled by absence: normal worker, check, watcher, CI, PR and
status commands do not import the metrics package and do not create metrics
state. Initialize it only in a project whose owner has selected the data scope.

## Start with explicit sources

```bash
orchestrator-engine --project-root /path/to/project metrics init
orchestrator-engine --project-root /path/to/project metrics sources register \
  --name local-runtime --type orchestrator-engine \
  --capability execution --capability delivery --capability usage
orchestrator-engine --project-root /path/to/project metrics collect \
  --source local-runtime
orchestrator-engine --project-root /path/to/project metrics report \
  --format markdown
```

`metrics collect` is a bounded read-only adapter for existing worker results,
usage records, local check results, workstream lifecycle, terminal events, host
delivery receipts and manual acknowledgements. It never edits those source
artifacts. Use `--dry-run` to inspect adapter counts and bounded errors before
ingestion. Successful non-dry collection advances a source-specific cursor, so
bounded calls eventually visit artifacts after the first page and later revisit
mutable snapshots. An unchanged snapshot is idempotent; changed content creates
a new immutable observation while retaining the same logical operation ID.

The short `--capability` form creates a conservative default inventory. A real
adapter should also identify `--adapter-version`, `--authority`,
`--identity-mapping`, `--observation-semantics` and a bounded
`--capability-inventory` JSON file. Each inventory row declares status, units,
counter semantics, precision, clock, freshness, permissions and collection
overhead. Inventory names must exactly match the declared capabilities.
`mutable_snapshots` and `immutable_events` replace prior data for the same
logical identity. A `mixed` source also defaults to a full snapshot; records
that intentionally add fields to one logical object must explicitly set
`data.observation_mode` to `complement` or `partial_update`. This prevents a
later acceptance snapshot from silently inheriting withdrawn evidence.

Other tools can register a source and submit normalized records without
coupling to OrchestratorEngine internals:

```bash
orchestrator-engine --project-root /path/to/project metrics record \
  --source local-runtime --record-type work_item \
  --data '{"work_item_id":"example-1","reworked":false}'

orchestrator-engine --project-root /path/to/project metrics ingest \
  --input /path/to/bounded-observations.jsonl
```

One import accepts at most 1,000 observations and each normalized observation
is at most 64 KiB. A source must be explicitly registered. Source IDs are
persistent UUIDs; account aliases are separate data and must not be treated as
project identity.

Use `metrics sources set-enabled --source NAME --disabled` to revoke a source
in a new registry generation. Current reports and guidance then exclude it and
new ingestion is rejected. Historical reports pinned to an earlier generation
remain reproducible; disabling a source does not delete its evidence.

## Storage and replay

The store lives under `.orchestrator/metrics/`:

```text
registry.json
objects/<prefix>/<sha256>.json
segments/<sha256>.json
generations/<sha256>.json
current.json
derived/
staging/
collectors/<source-id>.json
```

Observations, segments and generations are immutable content-addressed JSON.
Writers hold one advisory lock for at most five seconds. Objects are written
before an atomic `current.json` generation selector, so a crash before selector
publication leaves an invisible orphan rather than a partial visible report.
Readers verify the selected generation's complete hash closure and fail closed;
they never silently fall back to an older generation.

`metrics migrate` reports the current schema. `metrics migrate --recover` is an
explicit operator action that selects the newest verified generation and
repairs the selector. It does not delete orphan objects or rewrite historical
generations.

Every report pins the generation digest, registry digest, evaluation time,
event and knowledge cutoffs, cohort, formula catalog version and local access
scope. Replay a historical generation with `metrics report --generation SHA`.
JSON and Markdown are rendered from the same semantic report.
`metrics compare` evaluates both generations at one cutoff and rejects
cross-project, cross-policy, cross-cohort or cross-formula comparisons.

## Measurement semantics

Every metric keeps these axes separate:

- `availability`: whether the calculation has an authorized source;
- `evidence_class`: observed, estimated or unknown;
- `coverage`: expected, observed and unknown records plus a coverage state;
- `value_state`: known, a real zero, unknown, partial, estimated or not
  applicable;
- `unit`: never inferred or converted silently.

Provider-reported tokens are accepted only from complete provider-aware usage
records. Truncated or partial records remain partial lower-bound evidence;
missing usage is unknown, never zero. UTF-8 bytes are reported as bytes and are
not converted to tokens. Quota samples retain account alias, observation time,
reset timestamp and rounding semantics. A point-in-time quota sample is not a
promise that a later dispatch will be admitted.

Parallel execution uses both distinct attempt accounting and half-open elapsed
interval union. Mirrored observations with the same stable execution or event
identity are collapsed within their registered source. Native IDs from distinct
sources remain distinct. The built-in adapter also includes `execution_kind`
and `operation_kind` in native identity, so a worker and a local check with the
same text ID remain independent while a worker result and its usage complement
still combine. Cross-source mirrors collapse only when both producers
publish the same explicit `canonical_identity.namespace` and
`canonical_identity.id` and both sources declare
`identity_mapping = canonical_identity_authorized`. Shared and unfinished cost
remain visible and the reported buckets reconcile to the observed total;
per-accepted-outcome cost is undefined when no accepted outcome exists.
Canonical identity establishes equivalence only. It never transfers source
authority: guidance evaluates package bindings, final gates, obligations and
dispositions from the explicit project-owner record that asserted those fields.
Telemetry-only complements cannot inherit an acceptance claim from another
source.

## Metric catalog

| ID | Question | Built-in calculation | Producer status |
| --- | --- | --- | --- |
| `MET-001` | How many outcomes have explicit acceptance? | Accepted count plus project-owned scope progress | Generic source; synthetic integration verified |
| `MET-002` | How much non-overlapping execution occurred? | Half-open interval union plus optional empirical effort range | Worker/check adapter; synthetic integration verified |
| `MET-003` | How often did the first expected gate pass? | Passes over all expected starts | Generic source; synthetic integration verified |
| `MET-004` | How often was corrective rework recorded? | Reworked work-item ratio | Generic source; synthetic integration verified |
| `MET-005` | How much measured human attention was used? | Explicit seconds sum | Generic source; unavailable unless measured |
| `MET-006` | How much context was transferred? | Explicit byte sum | Generic source; no token conversion |
| `MET-007` | How many provider-reported tokens were used? | Complete usage-event sum | Worker usage adapter; partial data remains partial |
| `MET-008` | What quota remains for each account alias? | Latest explicit sample | Generic source; no provider query in core |
| `MET-009` | What fraction of attempts has real usage? | Known usage coverage | Worker usage adapter; synthetic integration verified |
| `MET-010` | How often did requested delivery succeed? | Delivered over terminal attempts | Event/receipt adapter; synthetic integration verified |
| `MET-011` | Which equivalent attempts added no evidence? | Explicit/bound repetition count | Generic source; advisory classification only |

Run `metrics explain` for the complete versioned definitions or
`metrics explain MET-007` for one definition and its limitations. A catalog row
means the calculation exists; it does not claim that every project produces the
required evidence. Each definition separately lists `calculated_fields`,
`evidence_view_fields` retained for inspection, and
`external_integration_fields` that require a project, host or provider producer.
The family name must not be read as a claim that every possible submetric in
that family is already calculated.

## Scope progress and forecasts

Scope reporting is opt-in and requires a separately registered project-owned
source. Runtime completion, a green check or a worker claim is not project
acceptance.

```bash
orchestrator-engine --project-root /path/to/project metrics sources register \
  --name project-plan --type project-scope --authority project_owner \
  --observation-semantics mutable_snapshots \
  --capability scope --capability acceptance

orchestrator-engine --project-root /path/to/project metrics record \
  --source project-plan --record-type scope_revision \
  --data '{"scope_revision_id":"plan-1","revision_index":1,"baseline":true}'

orchestrator-engine --project-root /path/to/project metrics record \
  --source project-plan --record-type scope_revision \
  --data '{"scope_revision_id":"plan-2","revision_index":2,"current":true}'

orchestrator-engine --project-root /path/to/project metrics record \
  --source project-plan --record-type scope_item \
  --data '{"scope_revision_id":"plan-2","work_item_id":"feature-1","criteria_revision":"criteria-2","module_id":"core","work_class":"feature","status":"in_progress"}'

orchestrator-engine --project-root /path/to/project metrics record \
  --source project-plan --record-type acceptance \
  --data '{"scope_revision_id":"plan-2","work_item_id":"feature-1","criteria_revision":"criteria-2","accepted":true,"evidence_ref":"checks/feature-1","completion_cycle_id":"feature-1-cycle-1"}'

orchestrator-engine --project-root /path/to/project metrics progress \
  --format markdown
```

Each revision has a non-negative `revision_index`; one revision may be marked
`baseline` and the newest revision marked `current`. Scope item states are
`planned`, `in_progress`, `blocked`, `review_ready`, `accepted`, `reopened` or
`removed`. Optional positive weights are project declarations. Missing weights
use one uniform item unit and the report exposes whether weighting is uniform,
owner supplied or mixed. Every scope item names a `criteria_revision`.

The current percentage uses only the current denominator. Baseline percentage
uses only acceptance explicitly bound to the baseline revision and is never
rewritten by a later revision's evidence. Additions, removals,
carryover, parent-linked splits and bounded change reasons remain visible, so
scope growth cannot masquerade as lost productivity. A later `accepted: false`
acceptance observation with the same scope and criteria applicability removes
the accepted contribution. An `accepted` scope status without an explicit
project-owned acceptance record, matching `scope_revision_id` and
`criteria_revision`, and `evidence_ref` or `evidence_digest` is reported but
does not count. Acceptance never flows implicitly into a later scope or changed
criteria. To carry evidence forward, write a new acceptance for the current
scope and criteria and optionally identify `carried_from_scope_revision_id`.

Forecasts require `work_class`, valid start/acceptance timestamps and at least
five comparable accepted completion cycles per remaining class by default.
Each forecast sample requires a stable `completion_cycle_id`; the same cycle
reaccepted across scope revisions counts once, and missing or conflicting cycle
identity does not increase the sample count. Use
`--minimum-samples` only as an explicit reporting policy. The deterministic
nearest-rank P50/P80 values are paired total-cycle samples, reported as
`sequential_effort_scenario_days_p50/p80`; they are scenario compositions, not
statistical quantiles of a parallel project finish date. Active and blocked
component distributions are shown separately and are not summed to fabricate a
total percentile. Dependencies are reported for coverage but
do not fabricate a critical path. Parallelism, work calendars and owner pauses
are not inferred, so the core does not emit an exact finish date. Use
`--module-id`, `--baseline-revision`, `--current-revision`, `--generation` and
`--evaluation-time` for reproducible bounded views.

## Deterministic guidance

```bash
orchestrator-engine --project-root /path/to/project metrics advise \
  --scope operation_only --operation-id CHECK-1 --format text
```

The advisor returns exactly one proposed next action, a content-bound guidance
snapshot ID and all known pending obligations. Package guidance requires an
explicit package ID and project-owned bindings; otherwise it returns
`package_context_unavailable`. It keeps historical verification, applicability,
live readiness and publication eligibility separate. A publication-eligible
answer requires owner-authorized final-gate evidence bound to the candidate,
check-plan revision and evidence digest; a worker's own success claim is not
enough. The source registry, not a payload field, establishes project-owner
authority. A package binding must also declare a complete bounded
`obligation_ids` manifest with `obligations_complete: true`; an absent
obligation record remains pending rather than proving completeness. A completed
or waived obligation must repeat the current requirement-set, candidate,
check-plan and policy applicability. An older candidate's obligation remains
historical and cannot close the current manifest. Conflicting current owner
bindings fail closed. The newest owner binding is selected before completeness
is evaluated, so an incomplete replacement cannot expose readiness inherited
from an older complete binding.

Operation-only guidance needs actual operation evidence. With no matching
facts it returns `operation_context_unavailable`; a completed focused operation
is handed back as an operation result and never invents a package final-gate or
publication decision. Queued and pending operations remain in flight. Unknown
or cancelled outcomes require inspection and are not handed off as complete.
Only the latest applicable attempt for an operation determines its current
outcome; an older success cannot mask a newer cancellation or unknown result.
Native operation IDs are grouped within their registered source and operation
kind. Use a positive `attempt_sequence` when a producer has a reliable retry
counter. Without one, the advisor uses `started_at`, then `effective_at`; mixed
or tied applicability fails closed as ambiguous. `known_at` still selects the
latest correction of the same stable execution, but does not reorder distinct
retry attempts. An accepted older final-gate attempt remains historical
verification but cannot authorize publication after a newer attempt is not
accepted.
Failed attempts remain historical, while only unresolved failures applicable
to the current candidate require repair. A project may
resolve or supersede a failure through an explicit project-owner disposition
record.

Evidence reuse must bind requirement, check-plan revision, candidate,
environment, source execution, evidence digest and policy revision. Missing or
invalidated bindings cause a refresh recommendation. A planned environment
transition can invalidate live readiness without erasing historical proof; a
foreign mutation requires restore or revalidation. A focused green result never
becomes final-gate readiness unless project-supplied final evidence says so.

Two equivalent attempts without useful evidence can trigger a diagnostic
recommendation. Changed inputs, a new hypothesis or useful evidence are not a
loop. Guidance is advisory and never cancels a command, suppresses required
verification, starts a duplicate, changes a provider or creates
`waiting_external`. If the metrics store or advisor is unavailable, follow the
ordinary project policy.

Guidance evaluates facts at one explicit `as_of` cutoff. Both `known_at` and
`effective_at` must be at or before that time, so future gates, dispositions and
obligation changes cannot affect current advice. Pass `metrics advise
--evaluation-time TIMESTAMP` to replay a pinned decision. Publication
eligibility is false whenever applicable work is running, incomplete,
ambiguous or resource-blocked, even when an older final gate remains valid
historical evidence.

The built-in delivery adapter carries the terminal event's raw operation ID and
operation kind into queue, receipt and acknowledgement observations. This link
is recovered from the durable event when an older receipt omits it, including
when event and receipt are collected on different cursor pages. Operation-
filtered reports therefore retain the complete delivery lifecycle.

## Privacy and operational limits

The metrics store is local runtime state and should normally remain ignored by
Git. Evidence pointers can contain local paths. Explicit exports are private by
default; review them before sharing. Do not ingest prompt bodies, private
documents, credentials, full logs or generated context payloads when bounded
identifiers and hashes answer the measurement question.

The optional [scale benchmark](../benchmarks/metrics_scale.py) generates only
synthetic records. It is not part of the normal test suite and makes no universal
performance claim. Record its hardware, record counts, archive size, peak
memory and timings when evaluating a deployment.

The checked-in [WSL2 reference run](../benchmarks/results/metrics-scale-wsl2-20260908.json)
records 100,000 observations, 10,000 attempts and 1,000 packages. It is evidence
for that host and implementation revision only. Pass an argv after
`--verification-command` to record bounded before/after exit, duration, size and
SHA-256 evidence for a project-selected paired verification command; command
output is not copied into the benchmark report.
