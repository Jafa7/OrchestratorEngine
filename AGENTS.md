# Agent instructions

These instructions apply to every AI client working in this repository.

## Scope

This repository provides a portable local orchestration layer for AI workers.
Keep product-specific policy out of the core package. Paradigmarium,
DocumentationEngine, Codex, Claude, Copilot and future clients must connect
through explicit adapters and documented contracts.

## Working rules

- Do not copy private planning documents from another project into this repo.
- Public documentation and code comments must be written in English.
- Preserve existing user changes.
- Do not commit, push, merge, rebase or perform destructive Git operations
  unless the user explicitly asks.
- Run git stage/commit operations from inside WSL for this checkout. Do not
  stage or commit from Windows Git over `\\wsl.localhost`; it can corrupt
  executable-bit metadata for scripts.
- Prefer deterministic file contracts, atomic writes and reproducible evidence.
- Add or update tests for behavioral changes.

## Risk-based verification

Before running checks, classify the change and use the narrowest level that
covers its risk:

- **Structural only**: prose documentation, comments, badges or repository
  metadata with no runtime, contract, packaging or generated-output effect.
  Do not run a test suite. Run only relevant structural checks such as parsing
  TOML/JSON, validating links or generated assets, and `git diff --check`.
- **Focused**: an isolated implementation or test change with a clear owning
  module. Run the directly affected tests and lint the touched code. Do not
  upgrade to the full suite merely because it exists.
- **Full**: shared contracts or schemas, CLI behavior, dispatch/watcher/state
  logic, dependencies, build/packaging/CI, cross-module behavior, release
  candidates, or uncertainty left by focused checks. During implementation,
  use focused checks; run the full gate only after the work is otherwise
  complete, immediately before handoff or release. Run:

  ```bash
  python -m unittest discover -s tests -p 'test_*.py'
  ruff check .
  git diff --check
  ```

Do not repeat an already-passing full gate after a later documentation-only
edit unless that edit changes generated artifacts, packaging inputs or test
expectations. For long checks, prefer the detached verification flow in
`docs/verification-policy.md`; on success read only the compact summary, and
open detailed logs only after a failure. If a dependency is unavailable,
report the blocker and run the checks that are available.

If a final full gate fails, inspect the failed check, fix with focused tests,
and run the full gate again only when a new final candidate is ready. Do not
run the complete suite after every intermediate edit.
