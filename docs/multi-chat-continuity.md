# Multi-chat continuity

Multi-chat continuity is an opt-in coordination contract for projects that use
several independently addressable AI chats. It solves a narrow failure mode:
one chat assigns work or review to another, ends its turn, and the project stops
because the result was never returned to the exact chat that owns the next
action.

The feature does not make turn termination meaningful. A silent or idle chat
is not evidence of completion, consent or abandonment. Progress is driven only
by explicit checkpoints, obligations, retained terminal results and fenced
ownership transitions.

## Model

One project-local SQLite database under
`.orchestrator/continuity/continuity.sqlite3` is the transactional authority for:

- dynamic actors, roles and versioned delivery endpoints;
- one owner, revision and control epoch for each work item;
- required or optional obligations between actors;
- idempotent request/reply envelopes with pinned recipient and return actors;
- assignment-scoped progress, waits and pauses;
- typed `all` or `any` waits over obligations, workers, checks, CI, pull
  requests or legacy workstreams;
- exactly addressed activations, consumer claims and a recoverable outbox;
- opt-in recovery observation with persisted incident backoff and explicit stops;
- bounded entry packets and idempotent operational diagnostics.

Legacy `workstream` files remain supported but are not a second owner for a
continuity work item. The watcher remains a deterministic delivery adapter. It
does not select the next task, interpret a review or run an AI dispatcher.

## Configure actors

Do not encode a universal number of chats. Register only the actors the project
actually uses. Codex endpoints are exactly addressable by task ID:

Start and verify the host-scoped Codex callback service before registering the
actors. `require-ready` rejects registration before durable actor state changes
if that point-in-time check is not ready.

```bash
orchestrator-engine --project-root /project continuity actor-register \
  --actor-id implementation --role implementation \
  --host codex --target-thread-id THREAD_IMPLEMENTATION \
  --completion-delivery-mode require-ready

orchestrator-engine --project-root /project continuity actor-register \
  --actor-id review --role architecture-review \
  --host codex --target-thread-id THREAD_REVIEW \
  --completion-delivery-mode require-ready
```

Changing an endpoint increments its generation, revokes unclaimed activations
for the old destination and reissues still-open work to the new destination.
It does not transfer ownership. Claude's current endpoint is session-bound and
cannot represent several exact concurrent Claude chats; use this flow only
where the host capability report provides suitable addressability.

## Managed requests and replies

Use a managed request when the sender must know whether a reply is still owed.
The engine never infers this from prose. `--requires-reply` atomically creates
the immutable request, its reply obligation and its outgoing outbox intent.
`--required` additionally makes that obligation gate work completion.

```bash
orchestrator-engine --project-root /project continuity request-send \
  --work-id FEATURE-1 --request-id FEATURE-1-REVIEW \
  --idempotency-key FEATURE-1-REVIEW-SEND-1 \
  --sender-actor implementation --recipient-actor review \
  --return-actor implementation --requires-reply --required \
  --message "Review the combined candidate and return a terminal response"
```

The recipient claims the delivered activation before responding. A receipt or
progress update is retained but does not resolve the reply obligation. A
terminal response stores the response, obligation result and exact return
activation in one transaction:

```bash
orchestrator-engine --project-root /project continuity request-respond \
  --request-id FEATURE-1-REVIEW --response-id FEATURE-1-REVIEW-RESULT-1 \
  --actor-id review --activation-id REQUEST_ACTIVATION \
  --kind terminal --terminal-status completed \
  --content "Accepted; no release-blocking findings"
```

The sender claims the return activation and explicitly records that it handled
the response:

```bash
orchestrator-engine --project-root /project continuity request-handle \
  --request-id FEATURE-1-REVIEW --actor-id implementation \
  --activation-id RETURN_ACTIVATION
```

Handling transport is not product acceptance. A terminal `failed` or
`cancelled` response closes the communication debt while retaining a non-success
product outcome. Repeating the same request idempotency identity or response ID
with the same content is safe; conflicting content or routing fails explicitly.
An informational request without `--requires-reply` creates no reply debt.
Unrelated owner checkpoints and product completion do not revoke a saved request
or terminal reply. If the return actor claims a reply and then ends its turn,
recovery may wake that exact return actor to inspect the saved response; it does
not transfer work ownership or require the responder to regenerate the answer.

## Implementation and review handoff

Create one work item and one explicit review obligation:

```bash
orchestrator-engine --project-root /project continuity work-start \
  --work-id FEATURE-1 --owner-actor implementation \
  --objective "Deliver the accepted feature contract"

orchestrator-engine --project-root /project continuity obligation-open \
  --work-id FEATURE-1 --obligation-id FEATURE-1-REVIEW \
  --requester-actor implementation --assignee-actor review \
  --resume-actor implementation --summary "Review the combined candidate"

orchestrator-engine --project-root /project continuity checkpoint \
  --work-id FEATURE-1 --actor-id implementation --expected-revision 1 \
  --mode waiting --summary "Candidate ready for review" \
  --wait-on obligation:FEATURE-1-REVIEW
```

The reviewer receives an entry packet containing the activation ID, work
revision, control epoch, obligation and exact claim command. It must claim
before acting, then record a terminal result:

```bash
orchestrator-engine --project-root /project continuity claim \
  --activation-id ACTIVATION --actor-id review --expected-epoch 1

orchestrator-engine --project-root /project continuity obligation-resolve \
  --obligation-id FEATURE-1-REVIEW --actor-id review \
  --activation-id ACTIVATION \
  --status completed --result-ref artifact://review/FEATURE-1
```

The resolved obligation satisfies the owner's wait and publishes one activation
to the implementation endpoint. The owner claims that activation before making
further changes. A stale revision or epoch fails closed.

Entry packets are bounded. They include the current typed wait, retained managed
results named by the activation, and the independently revisioned operational
note. If a work item has more obligations than fit in one packet, manifest
obligations and open required obligations are prioritized and
`obligation_summary.truncated` is true. Use the cursor from `continuity status
--work-id` to inspect additional obligations before deciding from an
intentionally truncated packet. Historical obligations do not impose a lifetime
limit on the work item.

Opening an obligation schedules a bounded reminder after 10 minutes by default,
so recovery does not depend on the reviewer first claiming the assignment. If
the reviewer resolves or cancels the obligation, pending assignments and
reminders are revoked. Otherwise a reminder can re-enter the exact reviewer
chat after its turn has ended. `--reminder-seconds` and `--max-reminders` are
explicit operator controls; reminders never continue indefinitely and do not
infer that silence means failure.

## Typed waits

Wait sources use `KIND:ID`. Supported kinds are `obligation`, `worker`, `check`,
`ci`, `pr` and `workstream`. Terminal state is acquired from validated retained
operation artifacts and is independent of whether the watcher has already seen
the corresponding transport signal. `all` waits activate after every source is
retained; `any` waits activate on the first unhandled result.

Claiming an activation is only an execution fence; it does not acknowledge that
the result was processed. After inspecting the result, persist a new waiting
checkpoint with its outcome identity:

```bash
orchestrator-engine --project-root /project continuity checkpoint \
  --work-id FEATURE-1 --actor-id implementation --expected-revision 2 \
  --mode waiting --summary "Review A handled; waiting for B" \
  --wait-on check:A --wait-on check:B \
  --handled-result result-OUTCOME_A
```

Acknowledged outcome identities are retained at work scope. They survive waiting,
pause, continue, ownership handoff and temporarily removing a source from the
current wait. A later result can activate the owner without replaying the first
one. A partially handled `all` wait counts acknowledged sources as ready but puts
only unhandled outcomes in the next activation manifest. Use
`--reprocess-result` only for an explicit decision to make a retained outcome
actionable again.

The immutable result artifact identifies one operation outcome. Descriptor and
evidence files may be published slightly later; reconciliation enriches the same
managed outcome with those bounded facts instead of treating publication order
as a new attempt. A genuinely new attempt must use a new operation identity or
produce a different immutable result.

Pause retains results but emits no execution continuation. Completion is
irreversible and is rejected while a required obligation is open. Optional
managed requests created before completion remain routable until they receive a
terminal response and the return actor handles it; this does not authorize new
product work. A completed non-request assignment may retain terminal evidence,
but it cannot publish a new product continuation. Ownership transfer requires an
expected revision plus explicit fencing evidence:

```bash
orchestrator-engine --project-root /project continuity transfer \
  --work-id FEATURE-1 --from-actor implementation --to-actor recovery-owner \
  --expected-revision 4 --reason "Isolated worktree handoff" --fenced
```

## Assignment-scoped progress

An assignee can checkpoint only its current claimed obligation without changing
the requester's work owner, revision or mode. This distinguishes a legitimate
long check from an unexplained gap and prevents one blocked review from freezing
another ready question:

```bash
orchestrator-engine --project-root /project continuity assignment-checkpoint \
  --obligation-id FEATURE-1-REVIEW-OBLIGATION --actor-id review \
  --activation-id REQUEST_ACTIVATION --expected-revision 0 \
  --mode waiting --summary "Waiting for the full gate" \
  --wait-on check:FEATURE-1-FINAL
```

The assignment modes are `continue`, `waiting` and `paused`. A new checkpoint
for `continue` or `waiting` increments the assignment generation and fences the
prior claim. A `paused` checkpoint retains the current claimed authority, so the
same assignee can explicitly resume it with a revision-fenced `continue` or
`waiting` checkpoint, or close it with a terminal response. Terminal retained
evidence satisfies only the linked assignment wait. If the assignee endpoint is
rebound while paused, the old claim is fenced and the replacement endpoint
receives an `assignment_paused_control` activation. Claiming that control does
not resume execution; it only authorizes an explicit revision-fenced checkpoint
or terminal cancellation/response.

Claiming an `assignment_wait_satisfied` activation does not acknowledge its
outcomes. Include each processed outcome in the next assignment checkpoint;
the assignment-scoped ledger prevents an `any` wait from replaying the same
result:

```bash
orchestrator-engine --project-root /project continuity assignment-checkpoint \
  --obligation-id FEATURE-1-REVIEW-OBLIGATION --actor-id review \
  --activation-id WAIT_ACTIVATION --expected-revision 1 \
  --mode waiting --summary "First check handled; waiting for the second" \
  --wait-mode any --wait-on check:CHECK-A --wait-on check:CHECK-B \
  --handled-result result-CHECK-A
```

Use `--reprocess-result` only to opt into replay of a specific acknowledged
outcome. Known local cycles between obligation waits are reported by
`continuity self-check`; the engine does not invent a resolution.

## Recovery and operation

Recovery observation is opt-in at the project level. Enabling it arms only new
managed work; existing unfinished work is not silently adopted:

```bash
orchestrator-engine --project-root /project continuity recovery-config \
  --enable --interval-seconds 60 --max-interval-seconds 3600

orchestrator-engine --project-root /project continuity recovery-adopt \
  --work-id FEATURE-1 --actor-id implementation
```

For a claimed assignment with no terminal response, checkpoint, typed wait or
explicit pause, or for a claimed owner continuation with no next work
checkpoint, reconciliation may enqueue one state-inspection activation.
This is allowed only when the host reports a safe sequential queue or correlated
terminal-turn observation. Codex currently provides the sequential queue path,
but not terminal-turn observation, so the reason is
`unconfirmed_checkpoint_or_response`, never confirmed abandonment. The consumer
must re-read current state and must not repeat already completed work.

Repeated empty inspections use persisted exponential backoff up to the configured
maximum. They do not consume an AI model while merely waiting, and there is no
hidden retry count, daily quota or lifetime work ceiling. Explicit work, actor or
project stops suppress every covered actionable request and continuation, not
only recovery. Incoming data remains durable, and an explicit resume reissues
the current routes from stored authority and endpoint generation without asking
a model to recreate messages or results. A claimed route that still belongs to
the current endpoint is not delivered twice; a route claimed by a superseded
owner endpoint is retained as audit history and replaced once for the new exact
endpoint. Owner checkpoints resolve only owner-continuation recovery incidents;
terminal-reply recovery remains pending until the exact request is handled or
explicitly cancelled:

```bash
orchestrator-engine --project-root /project continuity recovery-control \
  --scope work --scope-id FEATURE-1 --state stopped \
  --actor-id implementation --reason "User decision required"
```

`continuity status` exposes defaults, adopted policies, controls, due incidents,
next inspection times and capability-blocked records. An armed policy is a
technical observation preference, not permission to select new product work.

Run one watcher service for the project. The continuity outbox is reconciled on
watcher scans and can also be repaired deterministically:

```bash
orchestrator-engine --project-root /project continuity reconcile
orchestrator-engine --project-root /project continuity self-check
orchestrator-engine --project-root /project continuity self-check --repair
```

Self-check reports missing owner or assignee endpoints, malformed request debts,
known assignment cycles, missing or invalid typed sources, terminal sources not
yet recorded, claimed but unhandled results,
missing checkpoints or continuation routes, and failed outbox publication.
`self-check --repair` performs only mechanically justified transitions. An actor
may close a diagnostic with `continuity diagnostic-resolve`; the observed fact
and correction remain auditable, and an unchanged scan does not reopen it. A
changed finding receives a new identity and is reported again. A standard
repository backup should include the SQLite database, its WAL files and
continuity activation artifacts as one consistent state set.

Database initialization upgrades the previous continuity schema transactionally.
Existing live obligation assignments retain their assignment generation and a
recoverable current claim where one was already recorded. State-changing SQL uses
named columns so later schema additions cannot reinterpret positional values.
Legacy result digests, source-key handled cursors and wait activation manifests
are normalized once. Previously published outcome IDs remain valid aliases for
acknowledgement, so an upgrade does not make handled work actionable again.

Schema version 3 adds request/reply, assignment checkpoint and recovery records.
The v2 migration preserves actors, work, live claims, pending outbox rows,
canonical result aliases, handled-result ledgers and existing explicit control.
Historical work remains unarmed until `recovery-adopt` records that decision.

A claimed continuation suppresses replay only while its work revision, control
epoch and actor endpoint generation are current. Rebinding an owner leaves the
old claim as audit history and publishes the unhandled result to the new exact
endpoint; the stale activation cannot be claimed there.

Use `continuity note-update` for short-lived operator context that must not change
the work revision, ownership, control mode or next action. Result references are
kept separate from observed artifact facts: project-local readable files receive
a content hash and size, while unavailable or external references remain marked
unverified. These facts establish retention and applicability to the exact source
identity; they do not establish product acceptance.

There is no built-in DocumentationEngine adapter in the core package. A DE
reference is therefore an actor assertion unless an adopter supplies an explicit
adapter that can validate its revision and availability. An unavailable or stale
external reference remains visible, but it does not stop operational continuation
when the current entry packet already contains sufficient accepted inputs. Any
promotion of operational notes into durable product knowledge is a separate,
project-owned decision.

Actor registration is routing configuration, not proof that a delivery channel
will remain healthy. Arm the Codex watcher service and verify completion delivery
before ending a turn that depends on an activation. Readiness is point-in-time
evidence. The durable outbox permits replay after repair, but it cannot make a
stopped callback service notice its own backlog.

Consumer claims are a local coordination fence, not user authentication or a
filesystem sandbox. Every participant with write access to project state can
invoke the CLI, and fenced ownership does not stop an unrelated process from
editing the checkout. Use separate worktrees or host isolation when concurrent
writers are possible.

Do not use an app-level Goal while waiting for watcher delivery: it can keep the
host turn open and prevent a queued activation from becoming the next turn.
Do not add a timer that repeatedly wakes quiet chats. If progress requires a
person, checkpoint `paused`; if it requires a named operation or peer, checkpoint
`waiting`; if the next action is independently authorized, checkpoint
`continue` and end the turn. A `continue` checkpoint is deferred by 10 seconds
by default so the current host turn can close before delivery; use
`--delay-seconds` only when the host lifecycle requires a different bounded
grace period.

## Current boundary

Project-wide continuity status, reconciliation and self-check are maintenance
operations over retained work and wait state; their total execution cost is not
strictly bounded by the response page size. Prefer `continuity status --work-id`
for an operational decision, and run project-wide repair outside a latency-
sensitive handoff. Compact task diagnostics use an incremental projection, but
may still return every active, unresolved or large-log summary because hiding an
actionable task would be a worse default than a smaller response.

The host capability report currently declares native subagent observation
`unsupported`. There is no verified provider-retained source that a detached
observer can safely consume after the parent model turn exits. Use detached CLI
workers, local checks and exact chat endpoints instead. This limitation is
reported explicitly rather than hidden behind a best-effort bridge.

The same report separates durable enqueue, sequential queue processing,
consumer-start observation, terminal-turn observation and missed-event
reconciliation. These declarations are independent: queue acceptance is not a
consumer claim, and neither proves that an agent completed a useful response.
