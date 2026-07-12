# Risk-based verification policy

This policy helps agents choose enough verification for the changed behavior
without spending host-chat tokens and wall time on unrelated checks. It is a
portable project policy, not provider-specific core logic.

## Decision contract

Before running a command, classify the current change set at the highest
applicable level.

| Level | Use when | Required verification |
| --- | --- | --- |
| Structural | Prose docs, comments, badges, issue templates or repository metadata; no runtime, contract, packaging or generated-output effect | No test suite. Parse changed structured files, validate relevant links/assets and run `git diff --check`. |
| Focused | Isolated behavior or tests with a clear owning module and limited blast radius | Run directly affected tests and lint/type checks for touched code. Add a regression test for changed behavior. |
| Full | Shared schemas/contracts, CLI, dispatch/watcher/state, dependencies, packaging/build/CI, cross-module behavior, release candidate, or unresolved uncertainty | Use focused checks while editing, then run the complete project gate on the finished candidate, including install/build smoke when packaging is affected. |

User instructions and a project's own release policy can require a higher
level. An agent may also escalate after a focused failure suggests broader
impact. Convenience alone is not a reason to run the full suite.

## Verification timing

Focused checks provide the implementation feedback loop. Run them after the
relevant edits because they are quick and local. A required full gate is a
final-candidate check: run it after implementation, documentation and focused
regressions are complete, immediately before handoff, commit or release.

Do not run the complete suite repeatedly between intermediate edits. If the
final gate fails, inspect only the failing evidence, fix through focused
checks, and run the complete gate again when the updated work is once more a
final candidate. A failed gate never counts as the final verification, while
a passing gate should not be repeated without a scope-invalidating change.

## Examples

| Change | Level | Typical checks |
| --- | --- | --- |
| Fix wording in one Markdown file | Structural | Markdown/link review and `git diff --check` |
| Change badges or GitHub About text | Structural | Resolve badge URLs, read back repository metadata and run `git diff --check` |
| Edit `pyproject.toml` keywords only | Structural | Parse TOML; build metadata only if publication behavior matters |
| Regenerate a documented chart | Structural | Re-run the generator, compare JSON/SVG and parse both assets |
| Fix one status formatter | Focused | Formatter/status tests plus lint for touched Python |
| Add a durable JSON field or schema | Full | Schema, CLI and integration tests plus the complete gate |
| Change wheel/sdist contents | Full | Complete gate, build and installed-package smoke |
| Prepare a tag or GitHub Release | Full | Complete release gate once at the candidate commit |

## Gate validity

A passing gate remains valid until a later edit touches something in its
scope. Do not rerun a full gate after a prose-only follow-up when code,
contracts, generated assets, packaging inputs and test expectations are
unchanged.

Examples:

- full tests pass, then a README sentence is corrected: do not rerun tests;
- full tests pass, then `pyproject.toml` changes sdist contents: rerun packaging
  and install smoke, and use the full gate if preparing a release;
- focused tests pass, then shared schema code changes: the focused result no
  longer covers the change, so escalate to full.

Record the chosen level, why it applies, commands run and pass/fail status in
the handoff. Record `not run` for deliberately skipped suites; do not imply
that unrun checks passed.

## Long-running checks

Run long suites through a detached check runner when available:

1. Select `focused` or `full` before dispatch; do not let the check worker
   silently broaden the requested scope.
2. Finish implementation and focused checks before dispatching a full gate.
3. Dispatch from the chat that needs the result and end the current turn.
4. Store complete output in durable check artifacts.
5. On success, read only `verification-result.json` and `summary.txt`.
6. On failure, read the failed-command entry and its targeted log first. Read
   a complete log only when the compact evidence is insufficient.
7. Do not repeat a passing suite to produce a different summary.

This saves coordination context even on Codex Desktop, where live wakeup is
not currently reliable and status polling may still be required.

## Reusable agent-instruction snippet

Adopting projects can place this concise version in `AGENTS.md`, `CLAUDE.md`
or equivalent instructions and replace command names with their native gates:

```text
Use risk-based verification. For prose/docs/metadata-only changes, run no test
suite; validate only the changed structure and diff. For isolated behavior,
run focused owning-module tests and lint touched code. For shared contracts,
cross-module behavior, dependencies, packaging/CI, release candidates,
explicit user requests, or uncertainty after focused checks, run the full gate
once on the finished candidate, not after intermediate edits. If it fails, fix
with focused checks and rerun full only for the new final candidate. Do not
repeat a passing full gate after a later prose-only edit. Keep full output in
artifacts; report compact success, and inspect detailed logs only on failure.
```
