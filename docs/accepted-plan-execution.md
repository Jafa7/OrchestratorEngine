# Completing an accepted plan without artificial ceilings

Core mechanics preserve the accepted plan until it is finished, explicitly
stopped, or needs a real decision. A slice count, workstream age or arbitrary
token allowance is not completion evidence. New workstreams have no count or
total wall-time ceiling; optional owner-selected numeric limits remain supported.

## Core changes versus adopter configuration

| Concern | Responsibility |
| --- | --- |
| Optional workstream limits, atomic policy changes and stale-event protection | Core contracts and CLI |
| Actual provider usage collection without a token threshold | Existing core telemetry adapter |
| A configured global `dispatch.max_concurrent = 1` | Adopter choice, not a core default |
| Database, document and overlapping-file conflict boundaries | Adopter/host dispatch ownership and existing resource locks |
| Provider reset time and trustworthy availability probe | Provider/host adapter |
| Product scope, package readiness and acceptance | Project owner |

Omit `soft_token_budget` while retaining the appropriate `usage_adapter` to
record actual complete/partial usage. Existing explicitly configured soft
thresholds remain informational for compatibility; they never stop a process,
shorten a result or turn unfinished work into completion. Default example AI
profiles do not impose such thresholds.

Omitting `dispatch.max_concurrent` and per-profile `max_concurrent` permits
independent workers to run concurrently. Do not replace a global one-worker cap
with another arbitrary number. Explicit capacity limits can still be appropriate
when selected for a measured host/provider constraint. A concurrency count is
not a resource lock: schedule conflicting writes serially, use isolated
worktrees for independent changes, and acquire project-specific database or
document locks where required. Core does not infer file conflicts from prose or
promise automatic database/document locking. Review read-only tasks may run
alongside independent implementation when their reviewed revision is pinned.

## Quota recovery through existing worker and watcher contracts

An unavailable or rate-limited worker does not complete the workstream. Keep the
goal, next action, durable partial work and original failed task identity. Do not
rapidly retry model calls, and do not silently change provider/model or scope.

The optional [availability wait example](../examples/wait_for_availability.py)
uses the existing configured non-AI `availability_probe`. It sleeps until an
optional provider-supplied reset time, then probes with explicit exponential
backoff up to the selected retry interval. It invokes no AI worker or model.
Only `available` exits successfully; missing/broken probes exit with a failure
for diagnosis. Unknown capacity is never guessed as available. The profile is
reloaded before each probe, so disabling it prevents further probes.

Configure this deterministic helper as an ordinary worker in the adopter's
private `workers.toml`, using absolute paths selected for that installation:

```toml
[workers.capacity-wait]
command = ["/path/to/python", "/path/to/OrchestratorEngine/examples/wait_for_availability.py",
           "--project-root", "/path/to/project", "--worker", "implementation",
           "--retry-seconds", "300", "--maximum-retry-seconds", "3600"]
prompt_via = "stdin"
expect_long_running = true
```

The target `implementation` profile must already declare a bounded non-AI
availability probe. The example intervals are adopter choices, not engine
ceilings. Add `--not-before` with the actual reset timestamp if known. Do not
point this helper at itself or substitute an AI request for an availability
probe. An intentional total monitor timeout can still be configured.

1. Start one helper operation using a stable task ID, the accepted host binding
   and `--wake-policy always`. Keep its task prompt small and provider-neutral.
2. Record `waiting_external` on the original workstream, with
   `--waiting-on worker:CAPACITY-1` and the exact unfinished `--next-action`.
   End the host turn. The helper's terminal event is the sole completion route.
3. On that event, read current workstream and task status. Continue only if the
   workstream is still waiting for this exact operation and its successful
   result means availability was restored. A user pause, completion or replaced
   wait revokes the proposed next action; a late helper event cannot undo it.
4. The authorized owner agent resumes the same workstream, rechecks availability
   at dispatch and continues the retained action. Retry an unsuccessful worker
   through the existing `worker retry` lineage with its explicit reason and
   retry policy, or dispatch the next distinct task. Never start a duplicate
   while an old task is running, and do not declare product acceptance from the
   availability monitor's success.

Use existing `worker cancel` to cancel the helper process. Backoff, cancellation,
per-process timeouts and operation identity protections are retained. This
composition requires a functioning host wake adapter and configured probe; the
engine does not discover provider accounts or subscription reset times itself.

## Package verification boundary

An integration package includes every dependent slice needed for one finished
user outcome or contract, with no daily, time or numeric slice quota. A worker
completing its assigned slice does not make that whole package ready.

Run focused checks during implementation, including early verification of a
completed critical foundation. The package owner reviews the combined diff
after all dependent work is ready, then runs the required full gate. If it
fails, use focused checks for repairs and repeat the full gate on the next
finished candidate. Do not skip required verification to save tokens.

## Safe transition for an existing adopter

No adopter configuration, installed package or running watcher is changed by
these repository changes. In particular, do not write null limits while an old
watcher/CLI that expects integers still owns this state.

1. Inspect current workstream state, pending operation, active event and exact
   installed CLI/watcher versions. Keep existing numeric descriptors unchanged
   until all readers have been upgraded to support this contract.
2. In an explicitly authorized maintenance window, stop the old watcher, install
   the verified version for both the CLI and watcher interpreter, and restart
   with its existing state/binding. Do not delete checkpoints, signals or leases
   and do not restart active workers just to reset a workstream counter.
3. Read `workstream status`. Use `workstream set-policy --workstream-id ID
   --unlimited --expected-revision N --reason "Owner authorized accepted plan"`.
   Missing legacy `policy_revision` means zero. Confirm identity, checkpoints,
   counters, pending operation and active event are retained.
4. If active or waiting for an operation, leave the existing completion route
   intact. If stopped solely by the previous technical limit, the authorized
   owner can resume and create a new ready checkpoint. A policy edit itself
   never resumes paused/completed work or reauthorizes a revoked event.
5. Separately review adopter concurrency and soft budgets. Remove only artificial
   settings, retaining actual resource protection, provider backoff and explicit
   authorization boundaries. No changes to another project happen implicitly.

Historical numeric descriptors remain readable with their old semantics. Before
rolling back to an older reader, stop the new watcher and explicitly choose
finite limits through the new CLI; never let mixed-version readers share live
null-limit state. Prefer retaining the verified new reader for unlimited work.
