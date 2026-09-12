# Shared local resources

Version 1.6.0 includes this opt-in resource coordinator. It serializes conflicting
resource scopes across registered local projects while running independent
commands concurrently. It does not impose a worker, agent, daily-slice or model
token quota. An operation with no resource needs can run concurrently as well.

## Authority and enrollment

Choose one native host and one durable directory for each coordination domain.
Every path touching the same physical resource must use that authority. A
second database file with the same resource names is a different authority and
provides no mutual exclusion with the first. Do not share entire project state
directories to coordinate resources.

The authority uses standard-library SQLite with short `BEGIN IMMEDIATE`
transactions, synchronous durability and a service-instance file lock. Commands
execute in detached supervisors outside those transactions. The HTTP transport,
scheduler and result dispatcher have separate execution paths; slow delivery
does not hold resource ownership or prevent new grants. No managed project DB,
container platform or AI process is required by the coordinator.

Store the ledger on the authority's native local filesystem, outside disposable
worktrees and managed resources. UNC and known Linux foreign/network mounts are
rejected. On POSIX hosts, a pre-existing authority directory must be owned by
the current user with mode `0700`; initialization creates the SQLite ledger with
mode `0600`. The administrator remains responsible for avoiding network-backed,
synchronized or externally restored storage on every OS. Never open the ledger
simultaneously through Windows and WSL paths. Only native execution is supported:
Windows/WSL command bridges, escaped process groups, container processes and
remote executors require separately validated containment/quiescence adapters.

`resource init` accepts a private JSON configuration. Replace the example root
with a local absolute path, and use actual project-owned scripts:

```json
{
  "resources": {
    "database-a": {
      "physical_id": "local-test-instance-a",
      "incarnation": "1",
      "capacity": 1,
      "release": "probe",
      "probe": {
        "argv": ["{python}", "tools/assert_quiescent.py"],
        "timeout_seconds": 10
      }
    },
    "database-b": {
      "physical_id": "local-test-instance-b",
      "incarnation": "1",
      "release": "process"
    },
    "test-database": {
      "kind": "pool",
      "members": ["database-a", "database-b"]
    }
  },
  "projects": {
    "sample": {
      "root": "/absolute/path/to/sample",
      "recipes": {
        "verify": {
          "inputs": ["tools", "src", "tests"],
          "stages": [
            {
              "id": "build",
              "needs": [],
              "commands": [{"argv": ["{python}", "tools/build.py"]}]
            },
            {
              "id": "database-tests",
              "after": ["build"],
              "needs": [{"resource": "test-database", "mode": "exclusive"}],
              "commands": [
                {"argv": ["{python}", "tools/prepare.py"]},
                {"argv": ["{python}", "tools/test_database.py"]}
              ],
              "cleanup": [{"argv": ["{python}", "tools/cleanup.py"]}]
            }
          ]
        }
      }
    }
  }
}
```

`release: process` is an explicit assertion that ending all contained processes
establishes quiescence for that resource. Use it for synchronous local resources;
it is not a default assumption for asynchronous DB activity. `release: probe`
requires a registered, bounded, project-owned command that exits zero only when
old activity cannot interfere. A healthy or empty database is not necessarily a
quiescent database. A dirty but quiescent resource can be granted to a preparation
recipe; the coordinator does not mandate resets or expensive health gates.

Cancellation immediately revokes the ordinary work context. The current
runner receives a distinct epoch-scoped maintenance context for registered
cleanup and release probes, including after cancellation. That capability is
never passed to ordinary work commands and disappears when the stage releases
ownership. Cleanup and probes remain project code under the same local OS user;
this separation prevents accidental phase escalation but is not a hostile-code
sandbox.

Initialize and run the service in the selected native environment:

```text
orchestrator-engine resource init --directory AUTHORITY_DIR --config CONFIG.json
orchestrator-engine resource serve --directory AUTHORITY_DIR
orchestrator-engine --project-root PROJECT resource connect --directory AUTHORITY_DIR --project sample
orchestrator-engine --project-root PROJECT resource submit --recipe verify --id attempt-001 --wake-policy never
```

For a delayed launcher, retain the exact input contract before its process can
outlive the lock or transaction that selected those bytes:

```text
orchestrator-engine --project-root PROJECT resource create-input-contract --recipe verify --id attempt-002 --lineage attempt-001 --output RETAINED_INPUT_CONTRACT.json
orchestrator-engine --project-root PROJECT resource submit --input-contract RETAINED_INPUT_CONTRACT.json --wake-policy never
```

`resource-input-contract` schema version 1 binds the external request ID,
recipe digest, lineage and SHA-256 manifest. The file is not an authority token
and does not bypass a registered recipe. For a new request, the authority still
requires the registered root to contain exactly those bytes and captures them
again. If the live slot changed before an orphaned or delayed submit, admission
fails instead of recapturing the replacement under the retained request ID.
Once the same immutable request was accepted, replay is checked before live
inputs are read, so the same contract returns the existing request even if the
slot later changed or disappeared. A changed contract under the same request ID
is always a conflict. Create each retained file at a unique path; the command
refuses to overwrite an existing contract.
Store the file outside every path declared as a recipe input.

`init` returns configuration revision `1`. To add projects, change registered
recipes or change a drained resource registry, stop the service, ensure
`resource status` reports no active, waiting or recovery stages, and run:

```text
orchestrator-engine resource update --directory AUTHORITY_DIR --config CONFIG.json --expected-revision REVISION
orchestrator-engine resource serve --directory AUTHORITY_DIR
```

The compare-and-swap revision prevents overwriting a newer administrator
change. An update preserves authority identity and existing project bearer
credentials, permits new projects, and refuses project removal or root changes.
It also refuses every nonterminal stage, including waiting and
`recovery_required`; cancel or recover those attempts first. Existing v1.6.x
authorities without a stored revision are treated as revision `1` on their
first update.

Drain all resource stages before upgrading from a release that predates
phase-scoped maintenance capabilities. The engine never invents a capability
for an already persisted owner. A legacy active stage that reaches cleanup
without one fails closed and requires explicit recovery evidence.

`serve` runs until stopped; a local service manager may supervise it. It binds
only `127.0.0.1`, reuses its previous port after restart and refuses a second
service instance. `connect` creates private `.orchestrator/resources.json` in
the registered project. New connection files resolve the current endpoint from
the private authority directory, so an ordinary service restart does not require
another `connect`; reconnect once after upgrading an older connection file. Do
not commit that file or authority configuration.
Start or restart the authority outside every owned recipe/check/worker process
tree. A native containment boundary is required to terminate descendants, so a
service bootstrapped by contained project code is not durable even when that
child uses ordinary detached-process flags. Terminal runners independently
flush their durable outbox before exiting, which preserves the result and
follow-up event at this boundary; the authority must still be restarted by its
external service owner before later status, submission or scheduling calls.
On Windows, a custom external launcher must use `CREATE_NO_WINDOW` without
`DETACHED_PROCESS` or `CREATE_NEW_CONSOLE`; Windows ignores `CREATE_NO_WINDOW`
when either console-allocation mode is present. `CREATE_NEW_PROCESS_GROUP` may
be combined with `CREATE_NO_WINDOW`, and standard input/output/error must be
redirected. For example:

```python
flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
subprocess.Popen(
    [runtime_python, "-m", "orchestrator_engine.cli", *arguments],
    stdin=subprocess.DEVNULL,
    stdout=service_log,
    stderr=service_log,
    close_fds=True,
    creationflags=flags,
)
```

This launch policy controls console visibility; it does not move the authority
out of an enclosing Job Object. The service owner must already be outside every
owned operation tree.

Each project has a distinct bearer credential. Browser-origin requests and
environment-configured HTTP proxies are excluded. Clients can request only
registered recipes and inspect their own project; registration is a local
administrator action. Before sending that credential, a client verifies that
the process identity recorded for the endpoint still names the live authority
process. This protects stale endpoint files after a normal authority exit; it
does not turn loopback HTTP into a hostile same-user security boundary. This is
cooperative same-user coordination, not a sandbox against privileged users,
same-user state tampering or arbitrary direct database access.

The return value includes a `request` UUID. Use that UUID for:

```text
orchestrator-engine --project-root PROJECT resource status --request REQUEST
orchestrator-engine --project-root PROJECT resource wait --request REQUEST --timeout-seconds 30
orchestrator-engine --project-root PROJECT resource cancel --request REQUEST
orchestrator-engine --project-root PROJECT resource metrics
```

Choose one completion route. `never` supports a bounded in-turn wait. `always`
requires `--target-thread` matching the project's captured host binding, emits
one terminal follow-up through the existing watcher, and lets the chat end its
turn. Do not keep an app Goal active while relying on watcher continuation.

## First-class checks

A check suite can delegate its entire lifecycle to a registered recipe:

```toml
[suites.database]
verification = "focused"
resource_recipe = "verify"
```

Run normal `check plan`, `check run --suite database --check-id CHECK`,
`check status` and `operation wait --target check:CHECK`. Resource-managed checks
use detached execution and the default `.orchestrator` state directory. Explicit
foreground execution is rejected. A suite must select either `resource_recipe`
or local `commands`; there is no implicit wrapping of existing commands.
Checks retain their existing verification-result contract and operation owner.
The check reaper and supervisor refuse to take ownership away from the resource
authority. Cancel through `resource cancel` using its request UUID.

Manual launchers and check suites should submit the same registered recipe.
Do not enqueue a resource-dependent public launcher from inside a scope that
already holds that resource. Internal commands receive
`ORCHESTRATOR_RESOURCE_CONTEXT`, containing the exact request, stage, epoch and
selected members. The authenticated `/context` endpoint validates that token
against current running ownership; the environment value alone grants nothing.
Explicitly map concrete members to project resource connections in the adapter.

## Scheduling and input contract

Stages form an acyclic dependency graph. Declare the complete set for each
continuous scope. Preparation, tests and cleanup sharing mutable state belong
in the same stage. Resource-free preparation can run earlier. Parallel stages
share the captured workspace, so recipes must declare conflicting output paths
as resources or order those stages explicitly.

Leaves represent physical capacity. Aliases resolve to existing selectors;
pools choose one member; bundles require every component. Shared claims require
an explicit `compatibility` class and positive `units`; only matching shared
classes within capacity coexist. Exclusive access consumes the entire leaf.
No in-place upgrades or incremental acquisition while holding a partial set
are supported. Duplicate physical declarations, selector cycles, infeasible
claims and unknown dependencies fail before commands execute.

The scheduler orders eligible scopes by durable ready sequence and ID, searches
complete assignments lazily and passes blocked requests. Incompatible partial
assignments and mandatory leaves with insufficient intrinsic capacity are pruned
before expanding unrelated pool choices; the scheduler does not materialize the
Cartesian product of stage requirements. Explicit
search frames avoid imposing Python's recursion depth on the number of claims.
An older A+B request waiting for A initially permits useful B work. After a contested release, a still-ready
older scope can protect one concrete assignment while conflicting owners drain.
Younger reservations cannot conflict with it or transitively reserve unrelated
resources. Compatible work and unused capacity remain eligible. Protection is
removed on cancellation, lost readiness or recovery blocking. This intentionally
trades some temporary idle capacity for progress under repeated contention; no
runtime estimate or timeout is treated as a guaranteed backfill release.

Inputs are explicit relative files/directories. Symlinks and orchestration state
are excluded. Submission captures and verifies their SHA-256 manifest before
queue admission. Commands run in a unique captured workspace; `{python}` selects
the native interpreter and `{workspace}` selects that workspace. Source input
hashes are checked at each stage boundary. Generated outputs must use separate
paths. The manifest identifies actual bytes, including dirty files; it does not
claim that an arbitrary concurrently edited tree is one coherent Git revision.
The adopter must declare all relevant source/tool/configuration inputs and
freeze/export a coherent source set when that guarantee is needed. External
tools, services and undeclared files are not captured automatically.

The normal `resource submit --recipe ... --id ...` path intentionally hashes the
live root immediately before submission. Use `--input-contract` only when a
launcher has already retained the selected immutable manifest and may submit
later. Do not edit the contract: its digest detects accidental changes, while
the authority remains the final validator of recipe identity and source bytes.

Request IDs are project-namespaced and immutable. Exact replay returns the
existing attempt; an identical subscriber is a no-op, a new subscriber ID is
attached, and reusing a subscriber ID with a different pinned destination is
rejected. Changed inputs conflict. New retries use new IDs and may name
`--lineage PREVIOUS_REQUEST`. Explicit `subscribe` requires the returned complete
`contract_digest` and a subscriber JSON file with a unique `id` and pinned wake
destination. `unsubscribe` removes that subscription without cancelling the
execution. Executor cancellation is a separate project-authorized operation.
There is no automatic reuse of old green results or implicit cross-project join.

## Release, crash recovery and evidence

An atomic grant records all members and a single-use launch token. A native
supervisor waits behind a pipe barrier; its identity is recorded before admission
allows user commands. Rejected tokens cannot release another supervisor's grant.
An unadmitted release checks the exact epoch, launching state and token or
attached identity atomically with release. Service restart reconciles existing ownership and never
blindly repeats a launch. A live supervisor continues an admitted scope; new
stages wait for the service. Missing identity, uncertain launch or failed
quiescence quarantines the affected allocation. Neither heartbeat age nor
process disappearance alone releases it.

Cleanup runs after command failure/cancellation when containment is proven.
Native process groups or Windows Job Objects are swept at command boundaries.
Successful per-resource evidence allows partial release; equal `recovery_group`
labels couple release within the scope. Unknown resources remain unavailable
while unrelated resources can be reused. Resources deliberately shared across
independent scopes must have an adapter contract that makes those scopes safe.

An administrator can record recovery for an exact quarantined stage and epoch:

```text
orchestrator-engine resource recover --directory AUTHORITY_DIR --stage STAGE --epoch EPOCH --release RESOURCE --evidence EVIDENCE.json
```

The evidence must explicitly contain `quiescent: true` and a reason. This command
records a prior quiescence assessment; it is not an automatic DB repair. Never
clear ownership just because a timeout expired. Take backups only after stopping
submissions, draining all allocations and stopping the service. Restoring a
live/older ledger or transplanting it to another machine requires independent
resource reconciliation; automatic backup-rollback detection is not provided.
Do not delete an uncertain ledger and initialize another authority over live work.

Runner evidence is written before the release transaction. The same transaction
commits outcome, allocation release and terminal outbox eligibility. Delivery
then projects project artifacts and existing terminal/wake contracts using stable
subscriber event IDs. Each recovery incident has a separate immutable advisory
and event identity; it does not finalize a first-class check as failed. Successful
commands awaiting quiescence keep dependent stages waiting. Confirmed recovery
can unblock those stages and produce a distinct final result. An undelivered
advisory is superseded once its incident is resolved. Retries retain the captured
outcome snapshot rather than substituting later stage state. The shared generic
result is subscriber-neutral; subscriber-specific wake destinations remain only
in each durable outbox/event projection. Subscriber fields are validated at
admission, and a malformed check projection cannot stop other subscribers'
delivery. Delivery retries use deterministic non-AI backoff. Failed delivery
cannot rerun commands or hold already released capacity. Keep authority snapshots,
run evidence and ledger together; disposable test-output cleanup must not erase
that evidence. Retention/compaction is not automatic.

## Measurements and validation boundaries

`resource metrics` reports sample counts, ready-to-grant and grant-to-launch
delay, waits by reason, bypass count, oldest ready wait, recovery/protection
counts, resource ownership wall time, capacity-unit seconds and observed
release-to-next-grant delay. Shared owners' wall intervals are merged; their
capacity-unit costs are summed. Ownership includes quarantine and is not useful
CPU work. Per-project reports exclude other projects' private timelines.

Missing durations, model activity and unsupported idle attribution remain
`null`. Continuous executor-availability evidence is needed to distinguish
avoidable idle time from downtime or protection costs; this report does not
invent that evidence. Assignment search is exact and combinatorial: benchmark
large overlapping selector registries before adoption. No fixed project-worker
ceiling is used to conceal scheduling cost.

`tests/test_resource_queue.py` exercises deterministic scheduling, SQLite
concurrent writers, native admission, HTTP authorization, restart, first-class
checks, partial release, probe failure and delivery retry with synthetic resources.
The v1.6.0 release passed the suite on Linux, native Windows and macOS Intel/ARM,
including Python 3.11 and 3.13 in CI. See the
[exact-commit release verification](native-runtime-packages.md#release-verification).
The loopback service starts without reverse DNS, and the suite covers startup
with DNS unavailable. Project DB/bridge integrations and real sleep/reboot
recovery remain separate adopter acceptance work.
