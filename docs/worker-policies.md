# Worker behavior policies

Worker policies make efficient behavior explicit and auditable without moving
provider or product logic into OrchestratorEngine core. They guide AI workers;
deterministic check runners normally do not need them.

Host-side waiting is separate from worker policy. A host may block directly on
`worker wait` or use a narrowly scoped relay subagent to bridge a native wait,
but that relay does not inherit implementation, review or verification work.
The CLI worker remains responsible for its assigned outcome and risk-selected
verification. See [Codex in-turn continuation](codex-in-turn-continuation.md).

## Design goal

The objective is longer useful operation per provider allowance without lower
quality. The default policy therefore optimizes avoidable coordination work:

- repeated reads of unchanged files;
- broad repository exploration before it is justified;
- full test suites during intermediate edits;
- repeated passing checks;
- unbounded command output and full-log handoffs;
- continued work after acceptance evidence is complete.

It does not optimize by imposing a task token cap, skipping necessary files,
guessing through uncertainty or avoiding a required final gate.

## What core enforces

Core provides deterministic mechanics, not semantic judgment:

- safe config-relative policy paths with no absolute or `..` escape;
- stable policy names, at most eight files, 32 KiB per file, 64 KiB combined
  policy text and 8 KiB of JSON-compatible metadata;
- one dispatch-time read of policy bytes;
- an immutable task prompt copy, with explicit policy/task boundaries when a
  policy is selected;
- SHA-256 for the original prompt, effective prompt and every policy file;
- policy name/metadata/size/time in task evidence;
- hash verification before supervisor execution;
- an `info` diagnostic when an enabled profile has no policy.
- an `error` diagnostic before dispatch when a selected policy is unreadable
  or exceeds bounded control-plane limits.

The worker model still interprets natural-language policy. Core does not claim
that a prompt can enforce model behavior, count provider tokens or determine
whether a semantic answer is correct. Real quality remains established by
diffs, tests, schemas, review and other task evidence.

## What the default policy asks workers to do

1. Preserve the priority order: correctness, evidence, scope, economy.
2. Start with the smallest relevant context and expand on concrete dependency,
   contract, failure or uncertainty signals.
3. Reuse local project instructions, code and tools before inventing new
   abstractions.
4. Use structural/focused/full verification according to risk; reserve full
   gates for finished candidates.
5. Keep complete output in artifacts and return compact results first.
6. Escalate investigation for security, durable data, shared contracts,
   migrations, concurrency, packaging and ambiguous failures.
7. Stop after the requested outcome is verified, or return a concrete blocker
   instead of looping.

## Configuration and role overlays

Policy choice is explicit in `workers.toml`; OrchestratorEngine does not switch
to a weaker policy through a hidden cost heuristic.

```toml
[policies.implementation]
files = [
  "policies/quality-efficient.md",
  "policies/implementation.md",
]
quality_priority = "correctness-first"
context_strategy = "progressive"
verification_strategy = "risk-based-final-gate"
output_strategy = "compact-evidence"
role = "implementation"

[workers.codex-implementation]
command = ["codex", "exec", "--json"]
prompt_via = "arg"
policy = "implementation"
```

Files are composed in declared order. Put stable universal behavior first and
a small project/role overlay second. Define separate profiles for materially
different permission, model, effort or behavior combinations so the
orchestrating agent can select them explicitly. `worker list` and
`worker diagnose` expose `policy`, `policy_files` and `policy_metadata` for
that choice.

Recommended overlays are narrow:

- implementation: edit scope and acceptance criteria;
- review: findings-first, read-only behavior and no duplicate full gate;
- verification: requested suite only and compact artifacts;
- reporting: privacy-safe evidence and no product changes.

Do not copy private roadmaps, document bodies, credentials or large project
manuals into a policy. Project-specific instructions remain in the adopting
project, and private runtime policy/effective-prompt artifacts remain outside
public Git.

## Dispatch lifecycle

1. The host agent selects a worker profile whose model, permissions, cost and
   policy fit the task.
2. `worker run` atomically claims the task id.
3. Core reads the selected policy files once and composes
   `effective-prompt.md` with the task prompt.
4. Task/policy hashes and `wake_target` are written before supervisor spawn.
5. The supervisor verifies and executes the saved effective prompt.
6. Evidence preserves the exact policy and prompt hashes used by the worker.
7. The host reads compact result/evidence first and drills into full artifacts
   only when needed.

Changing `workers.toml`, a policy file or the original task prompt after step 4
does not change the effective prompt of that dispatched task.

## Migration

Existing profiles without `policy` continue to work. To adopt policies:

1. Run `adopt` on a new fixture. For an existing customized policy, export the
   installed reference with `worker policy export --name quality-efficient
   --output /tmp/quality-efficient.md`, compare it, and merge intentionally.
   Do not overwrite the adopter copy blindly.
2. Add `[policies.quality-efficient]` and assign
   `policy = "quality-efficient"` to AI worker profiles.
3. Run `worker list` and `worker diagnose --enabled-only`.
4. Dispatch a harmless smoke worker and inspect `task.json`,
   `effective-prompt.md` and `evidence.json`.

For AI dispatches, include `WORKER_TASK_INTENT.verification`. The generated
effective prompt treats that value as authoritative over generic or copied
verification prose. If a current explicit user instruction conflicts, dispatch
corrected intent instead of asking the worker to resolve the ambiguity.

Do not mass-edit existing durable task descriptors. Policy adoption affects
new dispatches only.
