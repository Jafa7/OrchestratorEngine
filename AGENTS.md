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

## Default verification

Run:

```bash
python -m unittest discover -s tests -p 'test_*.py'
ruff check .
```

If a dependency is unavailable, report the blocker and run the checks that are
available.
