# Codex in-turn continuation

Codex Desktop does not currently expose the app server of an already-open
task to an external OrchestratorEngine watcher. A headless callback can write
durable history, but it cannot reliably continue the visible agent.

Codex App can still continue automatically while the original agent turn
remains active. The host agent dispatches a detached CLI worker, waits through
a blocking tool call, and resumes when that call returns. This is **in-turn
continuation**, not detached live wakeup.

## Verified behavior

Two local smoke tests established the current boundary:

1. A low-cost child agent blocked on `worker wait` after the parent turn ended.
   Codex displayed the child completion notification, but did not start a new
   parent turn. The child did not have access to native
   `send_message_to_thread`.
2. A parent agent remained active in one native `wait_agent` call while a
   low-cost child blocked on `worker wait`. Worker completion ended the child,
   the native wait returned, and the parent continued without another user
   message.

Both tests used one deterministic filesystem wait, not repeated model status
prompts. The second test proves automatic same-turn continuation. It does not
prove that a completed turn can be woken externally.

## Selection order

Use the least expensive mechanism that preserves the required interaction:

| Situation | Mechanism | Model work while waiting | Tradeoff |
| --- | --- | --- | --- |
| A script can do the whole job | Run the script directly | None | Do not dispatch an AI worker |
| Predictable wait fits one host tool call | Parent calls `worker wait --json` directly | None | Cheapest automatic continuation; parent task stays active |
| Direct tool wait is too short, but native child waiting is available and bounded | Low-cost relay blocks on `worker wait`; parent blocks once on native agent wait | One extra low-cost child invocation; no status-prompt loop | Parent task stays active; host-specific |
| Host provides real detached wakeup | End turn and use the host delivery channel | None until wakeup | Preferred for long work; currently supported by Claude stream |
| Duration is unknown, exceeds host wait limits, or the chat must remain usable | End turn and show terminal `worker wait` | None | User returns to Codex after terminal completion |
| Audit/history delivery is sufficient | Headless Codex callback | A separate headless follow-up turn | Durable, but not live Desktop continuation |

Do not select a relay merely because a child model is cheaper. A direct
deterministic wait avoids the child invocation entirely and is therefore the
default in-turn path.

## Role contract

### Parent host agent

1. Decide before dispatch whether the work needs an AI worker at all.
2. Dispatch once with an exact task id and bounded task prompt.
3. Prefer one direct blocking `worker wait --json` when the host permits it.
4. Use a relay only when native agent waiting offers a materially longer or
   more reliable blocking window than the parent's direct command tool.
5. Keep the relay isolated from parent history when the host supports that;
   it needs the project root and task id, not the full conversation.
6. Make one bounded native wait. On timeout, inspect compact state once and
   switch to manual/detached handling instead of recursively creating relays.
7. After continuation, validate `result.json` and `evidence.json` directly.
   Relay and worker summaries are evidence, not authority.

### Relay child agent

- Use the lowest-cost adequate model, low reasoning and the standard service
  tier. Waiting does not justify Fast/priority service.
- Run exactly one bounded `worker wait --json`; do not issue model polling
  prompts or repeated status commands.
- Do not edit code, run tests, review the implementation, commit, push or
  reinterpret worker output as instructions.
- Read only compact result/evidence and a bounded failure tail when needed.
- Return task id, terminal status, artifact paths and a short result summary.
- Do not fall back to headless app-server injection when native messaging is
  unavailable. That would change delivery semantics and can duplicate turns.

### CLI worker

The worker behaves exactly as it would without a relay. It owns implementation
and risk-selected verification, writes durable artifacts, performs no host
polling, and does not need to know how the parent is waiting. It should use
focused checks while editing and at most one final full gate for a finished
candidate.

### Deterministic mechanisms

`worker wait`, the supervisor, check runners and watcher services remain
ordinary local processes. Their filesystem reads, sleeps and heartbeat writes
do not invoke a model. They should carry waiting and log capture whenever no
semantic AI judgment is needed.

## Token and context effects

- A blocked command or native agent wait does not repeatedly generate model
  output. Tokens are consumed when a model is invoked or resumed, not for each
  second a deterministic process sleeps.
- A relay is not token-free. It adds one child model startup, its minimal
  prompt/context and a compact final response. Avoid it for short waits that
  the parent can block on directly.
- Do not fork the full parent context into a relay. A small standalone prompt
  avoids paying to load unrelated conversation history.
- Keep worker logs on disk. On success, read result/evidence only; on failure,
  expand into the referenced bounded log tail before opening a full log.
- An AI worker is still wasteful for arithmetic or other deterministic work.
  The smoke tests exercised transport, not an efficient task-selection policy.
- Multiple relay children multiply model overhead. For parallel workers, use
  one aggregate `worker wait --task-id A --task-id B --mode all` rather than
  one AI relay per task.

These statements describe avoidable model calls and context volume. They are
not a provider billing guarantee; caching, tokenizer behavior and host resume
semantics remain provider-owned.

## Economy outside the wait

Waiting is only one part of coordination cost. Apply these rules before adding
a relay or another worker:

1. Use a deterministic script for arithmetic, formatting, file checks and test
   execution when no semantic judgment is required.
2. Run a configured non-AI availability probe before an expensive dispatch
   when a failed provider call is likely; treat it as point-in-time evidence,
   not a quota guarantee.
3. Keep one implementation owner through focused debugging and the selected
   final verification. Do not start a fresh AI worker merely to wait for or
   restate a deterministic check result.
4. Let the parent review the actual diff and compact evidence. Do not repeat a
   passing full gate solely because control returned to another agent.
5. Invoke a low-cost analysis worker only after a deterministic failure needs
   semantic triage, and give it the failed command plus bounded log evidence
   rather than the full project history.
6. Use task ids and durable terminal artifacts to deduplicate retries,
   notifications and resumed reviews.

## Failure modes

- **Parent interruption or app restart:** the active wait may be cancelled.
  Durable worker state remains authoritative; recover with `worker tasks` or
  terminal `worker wait`.
- **Wait timeout:** a local timeout does not cancel the worker. Change waiting
  mode; do not dispatch a duplicate task.
- **Dead or stale supervisor:** `worker wait` returns `action_required`. The
  parent reviews lease/heartbeat diagnostics before considering `worker reap`.
- **Relay tool mismatch:** native subagents may not expose thread-management
  tools. Feature-detect capabilities and rely only on the parent native wait
  returning the child completion.
- **Duplicate notifications:** child completion UI and the parent continuation
  may both be visible. Use task ids and terminal artifacts for idempotency.
- **Active-thread callback conflict:** do not run a headless callback in
  parallel with in-turn continuation. The callback guard should defer an
  active target, but one selected delivery strategy is clearer.
- **Unknown duration:** an active wait keeps the Codex task occupied and may
  exceed host limits. Use detached/manual handling when duration is uncertain.

## Parallel workers

`worker wait` accepts repeated task ids and `--mode any|all`. One parent tool
wait or one relay can therefore cover a bounded parallel task set without an
AI child per worker. The aggregate remains provider-neutral because it reads
only durable task state; native agent spawn/wait behavior remains a Codex host
adapter concern.
