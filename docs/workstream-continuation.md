# Workstream continuation

OrchestratorEngine can return an agent to an already accepted body of work,
with explicit, auditable continuation and owner-selected optional limits. The
watcher is only the delivery transport. It does not interpret roadmaps, choose product
work or infer intent from an agent turn ending.

## Contract

Start a workstream from the host chat that owns the work:

```bash
orchestrator-engine --project-root /path/to/project workstream start \
  --workstream-id ROADMAP-1 \
  --goal "Complete the accepted roadmap" \
  --delay-seconds 10 \
  --unlimited
```

The command snapshots the current binding. Later continuation signals remain
routed to that host target even if the project is rebound.

## Host turn requirement

Do not combine watcher-driven continuation with an app-level Goal that keeps
the host turn active. The watcher can queue a completion message, but that
message cannot become the next turn until the current turn ends. This can make
a healthy worker, check or CI monitor look as though it failed to wake the
chat.

For autonomous work that must resume after an external operation:

1. Store the accepted objective and limits with `workstream start`.
2. Start the worker, check or monitor as a detached operation.
3. Record a `waiting_external` checkpoint naming that operation.
4. End the host turn and let the operation's terminal event wake the chat.
5. On resume, read the checkpoint and bounded result/evidence before acting.
   For a timer continuation, also confirm that `workstream status` is `active`
   and its `active_continuation.event_id` matches the received event. A queued
   message can outlive the state that originally authorized it.

An in-turn blocking wait is a separate mechanism. Use it only when the current
turn is intentionally kept open and no watcher message is expected to resume
that phase.

At a genuine phase boundary, an agent records exactly one decision:

- `continue`: a concrete next action is ready and no user decision or external
  prerequisite is pending;
- `waiting_external`: a named worker, local check or CI operation owns the next
  transition and its own terminal event should wake the chat; pass that
  identity with `--waiting-on`;
- `needs_user`: a user decision or authorization is required;
- `blocked`: progress cannot continue from currently available inputs;
- `complete`: the accepted workstream goal is complete;
- `paused`: continuation is intentionally stopped without declaring success.

Only `continue` schedules a follow-up:

```bash
orchestrator-engine --project-root /path/to/project workstream checkpoint \
  --workstream-id ROADMAP-1 \
  --checkpoint-id phase-2-ready \
  --decision continue \
  --summary "Phase two is implemented and focused checks pass." \
  --next-action "Perform the independent review and final gate." \
  --ready
```

`--ready` is an explicit agent declaration that the next action is within the
user-approved scope, requires no unresolved user choice and has no unfinished
external prerequisite. Task prose alone cannot provide this declaration.

## Safety boundaries

- Ending a chat turn does not imply continuation. Absence of a checkpoint is
  always absence of authorization.
- App-level Goal state is not a workstream contract and must not be used to
  retain a turn that is waiting for watcher delivery.
- Starting a workstream requires an existing host binding. Continuation never
  falls through to whichever chat happens to bind the project later.
- The default delay is 10 seconds. The watcher ignores the durable signal
  until its `not_before` timestamp, without model polling.
- A checkpoint ID is immutable and idempotent. Repeating the same content is a
  no-op; reusing the ID for different content fails.
- New workstreams default to no continuation-count or total wall-time limit.
  `--unlimited` states this explicitly; omitted limits serialize as JSON null.
  There is no daily slice quota. Counters still record every automatic phase.
- An owner may explicitly set positive `--max-continuations` and/or
  `--max-wall-seconds`, without the former 100/604800 ceilings. Existing numeric
  descriptors keep their limits and interpretation. Timer `continue` and
  `waiting_external` checkpoints both count, and explicit wall-time includes
  waiting. Reaching an explicit limit records `needs_user` and suppresses timer
  continuation; it never declares the accepted plan complete.
- `needs_user`, `blocked`, `waiting_external` and `paused` require an explicit
  `workstream resume` before another `continue` checkpoint.
- A completed workstream cannot be resumed.
- The contract does not authorize commit, push, merge, release, publication,
  destructive actions or expansion beyond the user's accepted scope.
- Core does not parse roadmap documents or create tasks from project content.
  Adopters may export a small machine-readable ready-work item and let the
  host agent validate it before declaring `--ready`.
- `waiting_on` is a bounded operation identity used for audit and agent
  verification. It is not proof that the named operation exists or owns a
  compatible wake channel; start and verify the detached operation before
  recording the checkpoint.

Do not checkpoint between tiny sequential edits. Continue within the current
turn while context is useful. Use a checkpoint at a phase boundary, before a
long idle period, after an external result, or when a compact handoff is less
expensive than retaining a growing conversational context.

## Durable state

Each workstream uses:

```text
.orchestrator/workstreams/<workstream_id>/
  workstream.json
  checkpoints/<checkpoint_id>.json
  artifacts/results/<checkpoint_id>.json
  artifacts/evidence/<checkpoint_id>.json
```

The result contains only the bounded summary, next action and due time. The
generic terminal event and inbox signal contain artifact paths and hashes.
These local artifacts may contain private planning context and must remain out
of public Git under the adopter's retention policy.

Checkpoint, descriptor and event publication is a recoverable transition. On
each scan the watcher reconciles a recorded checkpoint that was interrupted
before descriptor or signal publication. The descriptor holds one
`active_continuation`; a later stop or continuation checkpoint revokes the
older timer signal before host delivery. New operation identities use
`workstream:<workstream_id>:<checkpoint_id>`; legacy checkpoint events remain
readable and recoverable. Legacy result and evidence files stored beside
checkpoints also remain readable, while new generated artifacts use separate
directories so every valid checkpoint ID has an unambiguous path.

Read compact state with:

```bash
orchestrator-engine --project-root /path/to/project workstream status \
  --workstream-id ROADMAP-1
```

After the user or an external result resolves a stop condition, resume from
the active host chat:

```bash
orchestrator-engine --project-root /path/to/project workstream resume \
  --workstream-id ROADMAP-1
```

## Changing an existing policy

Use the supported command instead of editing a live descriptor:

```bash
orchestrator-engine --project-root /path/to/project workstream set-policy \
  --workstream-id ROADMAP-1 --unlimited \
  --expected-revision 0 --reason "Owner authorized completing the accepted plan"
```

The revision is shown by `workstream status`; legacy descriptors without
`policy_revision` have revision zero. The optional expected revision rejects a
stale concurrent edit. Repeating an unchanged policy is a no-op. You can also
set either positive limit independently, or remove just one with
`--no-continuation-limit` / `--no-wall-time-limit`. Omitted fields stay unchanged.

The API is `set_workstream_policy(project_root, workstream_id=...,
limits={"max_continuations": None, "max_wall_seconds": None}, reason=...,
expected_revision=...)`. Each effective change atomically records before/after
limits, reason, timestamp and monotonically increasing revision in the
descriptor's `policy_history`, under the same lock used for delivery.

A policy update does not reconcile checkpoints, emit an event, resume a stop,
change the goal/host binding, reset counters or remove pending operation and
`active_continuation` state. Count limits govern future automatic checkpoints;
an already authorized pending timer remains authorized. Explicit wall-time
continues to be checked at delivery. Cancellation, later stop checkpoints and
stale event checks still override pending timers.

If the workstream was stopped solely because an old limit was reached, an
authorized owner agent can issue `workstream resume` after changing the policy
and record a new ready checkpoint. This does not require a successor workstream.
Do not resume a separate user pause, cancellation or unresolved decision merely
because limits were removed. An already revoked event is never reauthorized.

See [accepted-plan execution and migration](accepted-plan-execution.md) for
provider waits, independent concurrency and a safe adopter transition.
