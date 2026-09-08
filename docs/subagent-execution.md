# Subagent execution policy

This policy defines how a parent agent, native subagent, detached CLI worker
and deterministic runner share work without lowering verification quality or
spending model calls on process monitoring. It is provider-neutral. Host
adapters may expose different spawn and wait APIs, but the ownership and
evidence rules remain the same.

Natural-language policy guides agents; it cannot enforce a provider's native
subagent behavior. OrchestratorEngine enforces only its own durable task,
operation, evidence and delivery contracts.

## Roles

| Role | Owns | Must not do |
| --- | --- | --- |
| Parent host agent | User contract, dispatch decisions, final acceptance and authorization boundaries | Treat child output as authority or silently expand the user's scope |
| Implementation subagent | One bounded implementation and the verification needed to finish it | Report completion while required checks are pending or hand work to an unnamed successor |
| Review subagent | Independent, normally read-only review of a named change | Repeat the implementation, run an unrelated full gate or edit without authorization |
| Diagnosis subagent | Semantic analysis of a bounded failed-command evidence set | Monitor the running process, ingest unbounded logs or claim the fix is verified |
| Relay subagent | One bounded wait and a compact terminal handoff | Edit, test, review, diagnose, recursively delegate or reinterpret results |
| Detached CLI worker | Its assigned outcome and risk-selected verification | Depend on the parent polling it or omit acceptance evidence |
| Deterministic runner | Command execution, waiting, timestamps, exit codes and durable logs | Make semantic product decisions or claim model-level quality |

Choose a model for the semantic difficulty of its role, not for how long an
external command may run. Waiting alone never justifies a stronger model or a
priority/Fast service tier.

## Dispatch contract

Before starting a subagent, the parent supplies a bounded contract:

1. Stable task or operation identity.
2. Role and concrete outcome.
3. Allowed code, documentation or evidence scope.
4. Acceptance criteria and verification baseline.
5. Permissions and explicit commit, push, network or release authorizations.
6. Completion route: continue in the same agent or hand control to the parent.
7. Stop conditions requiring user input, unavailable dependencies or scope
   escalation.
8. Whether further semantic delegation is allowed. The default is no recursive
   delegation.

Do not copy the full parent conversation when a task file, changed-file list
and acceptance contract are sufficient. Do not omit context that is necessary
for correctness merely to reduce prompt size.

## Ownership invariant

There is one implementation owner for a bounded change at a time. Starting a
test, detached operation, relay or review does not transfer ownership. The same
owner normally reads a failed focused check, fixes the code and verifies the
repair while its working context is still useful.

Ownership changes only through an explicit handoff that records:

- the next owner, normally the parent chat;
- the exact task or operation id;
- current changed-state and verification state;
- compact result and evidence paths;
- the next required decision.

An implementation is not `completed` while required verification is running.
Use `waiting_external` or an equivalent non-terminal handoff state. The parent
remains responsible for final acceptance even when a worker reports success.

## Check and wait selection

Run `runtime-capabilities` before relying on detached execution and use `check
plan` for the configured suite. The plan combines measured or configured
duration with verification class: an unknown full gate recommends detached,
while an unknown non-full check recommends foreground. A limited platform may
run foreground checks but rejects detached lifecycle commands. Execution mode
never transfers ownership by itself.

| Situation | Execution | Agent behavior | Wake policy |
| --- | --- | --- | --- |
| Structural-only change | Structural checks only | Continue in the same owner | No wakeup |
| Plan selects foreground | One foreground blocking call | Continue when the call returns | No wakeup |
| Plan selects detached, runtime supports it and active debugging needs the result | Deterministic detached runner plus one bounded wait | Same implementation owner reads the result and continues | `never` |
| Plan selects detached and implementation is otherwise ready for parent handoff | Deterministic detached runner | Child hands the operation to the parent; parent ends its active turn | One terminal wakeup |
| Detached lifecycle is unsupported | Supported foreground call when its wait is acceptable; otherwise explicit manual/host-native handoff | Do not claim detached wake delivery | No engine wakeup |
| External CI or PR readiness | Detached deterministic monitor | Hand off to parent unless an in-turn wait is deliberately bounded | One terminal wakeup |
| Several parallel operations | One aggregate `operation wait --mode any|all` | Do not create one relay per operation | Matches the selected single route |

For same-owner continuation, start the check without wake delivery and wait
once for the current decision phase on its durable identity:

```bash
orchestrator-engine --project-root /path/to/project runtime-capabilities
orchestrator-engine --project-root /path/to/project check plan --suite focused
orchestrator-engine --project-root /path/to/project check run \
  --check-id FOCUSED-1 --suite focused --execution auto --wake-policy never
orchestrator-engine --project-root /path/to/project operation wait \
  --target check:FOCUSED-1 --mode all --json --timeout-seconds 900
```

If `check run` selected foreground execution, it already returns the terminal
result and the second command is unnecessary. If it selected detached
execution, the wait reads bounded state and sleeps in an ordinary process; it
does not invoke a model between intervals.

For parent handoff where either outcome needs a parent decision, use `always`
or an equivalent aggregate policy that emits one terminal wake for success and
failure, then record the operation id. `on-failure` is valid only when success
requires no parent continuation. The child returns that handoff to the parent
instead of claiming completion. The parent verifies the identity, records `waiting_external` when a
workstream is active, and ends its own host turn. Only then can a queued watcher
message become the next turn. The wake target is the parent host chat, not the
disposable child. Do not keep an app Goal active and do not also block on the
same wake-enabled operation.

## Debugging loop

An implementation subagent uses this loop:

1. Inspect the smallest code and contract surface that can affect the task.
2. Make one coherent change.
3. Run the smallest focused check capable of disproving it.
4. On failure, read the compact failed-command evidence first.
5. Open only the referenced bounded log section needed to diagnose the cause.
6. Fix and repeat the focused check when the change invalidated its evidence.
7. Run a required full gate once on the finished candidate.
8. Hand off changed files, actual checks, artifacts and residual risk.

The number of focused iterations is driven by evidence, not an arbitrary token
cap. Do not run the full suite after every edit, and do not skip a necessary
check to save tokens.

## Timeout and failure behavior

- Wait exit `124` means the operation is still active. It is not a failure and
  does not authorize a duplicate launch.
- One bounded wait is allowed per explicit decision phase. After a wait limit,
  hand the existing operation id and returned state to the parent. The parent
  may deliberately start a new decision phase with another bounded wait, but
  the child must not repeat it automatically or enter a status/sleep/log loop.
- `action_required` means inspect lease, heartbeat or descriptor evidence
  before deciding whether recovery or reaping is safe.
- A failed check starts with its compact summary and failed-command entry. Read
  the full log only when bounded evidence is insufficient.
- A lost chat or interrupted wait does not lose the durable operation. Recover
  by identity; never redispatch merely because a UI notification was missed.
- Delivery failure does not change the recorded execution result. The parent
  can recover from durable result/evidence and acknowledge the event.

## Role-specific completion

### Implementation

Return only after the bounded outcome is implemented and verification is
complete, or explicitly hand off a still-running operation. Include changed
files, checks actually run, evidence paths, blockers and residual risk. Do not
claim parent-level acceptance, commit or publication unless authorized.

### Review

Return findings first with precise references. Run only focused checks needed
to validate a concrete finding. Do not repeat a passing final gate merely for
independence; inspect its durable evidence. A no-findings result must still
state untested areas and residual risk.

### Diagnosis

Receive the failed command, compact result and bounded log evidence. Return a
probable cause, confidence, smallest suggested next check and relevant paths.
Do not edit unless the task explicitly changes the role to implementation.

### Relay

Run exactly one bounded `worker wait` or `operation wait`, then return identity,
terminal state and artifact paths. A relay is a host-control workaround, not a
test runner. It is allowed only when the task explicitly authorizes this role
and direct parent waiting plus detached wake delivery cannot provide the needed
bounded bridge. Never create another relay.

## Token and quality safeguards

- A deterministic process sleeping inside one blocking wait does not generate
  repeated model turns. Provider accounting for the initial and resumed calls
  remains provider-owned.
- Store full stdout/stderr on disk. On success, expose only status, duration,
  command identity and artifact paths. Expand failure evidence progressively.
- Reuse a still-valid passing result. Rerun only after a change invalidates its
  scope, environment or acceptance assumptions.
- Preserve one implementation context through active debugging when practical.
  A fresh agent must receive a compact durable checkpoint instead of rereading
  the entire history.
- Economy never lowers the required verification level, suppresses uncertainty
  or turns a partial result into success.

## Reusable instruction snippet

Adopting projects may place this compact form in `AGENTS.md`, `CLAUDE.md` or an
equivalent host instruction file:

```text
Keep one implementation owner through its debugging loop. Do not poll tests or
long commands with repeated model turns. Use check plan and runtime-capabilities
to select supported execution. If the same owner needs the result, disable wake
delivery and use one bounded deterministic wait for that decision phase. End a
subagent only through an explicit handoff naming the operation and parent. The
parent then records waiting-external state and ends its active turn so one
terminal wakeup can resume it. A pending required check is not completed work,
and a timeout does not authorize a duplicate or automatic second wait. A relay
is an explicitly authorized host fallback, never the default. Keep full logs in
artifacts, read compact success, and expand only failed evidence.
```
