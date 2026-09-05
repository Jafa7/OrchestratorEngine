# Bounded workstream continuation

OrchestratorEngine can return an agent to an already accepted body of work,
but continuation is always explicit, bounded and auditable. The watcher is
only the delivery transport. It does not interpret roadmaps, choose product
work or infer intent from an agent turn ending.

## Contract

Start a workstream from the host chat that owns the work:

```bash
orchestrator-engine --project-root /path/to/project workstream start \
  --workstream-id ROADMAP-1 \
  --goal "Complete the accepted roadmap" \
  --delay-seconds 10 \
  --max-continuations 8 \
  --max-wall-seconds 14400
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
- A workstream defaults to at most eight automatic resumptions and four hours.
  Both timer `continue` checkpoints and `waiting_external` checkpoints consume
  this shared budget. Reaching either limit records `needs_user` and emits no
  timer wakeup; a timer signal that becomes due after wall-time is suppressed.
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
  checkpoints/<checkpoint_id>.result.json
  checkpoints/<checkpoint_id>.evidence.json
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
readable and recoverable.

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
