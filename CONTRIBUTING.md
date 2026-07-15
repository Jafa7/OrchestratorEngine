# Contributing to OrchestratorEngine

OrchestratorEngine is a portable, provider-neutral and adopter-neutral
coordination layer. Contributions should preserve deterministic contracts,
durable evidence and clear adapter boundaries.

## Adopter-neutral public content

Public product documentation, contracts, fixtures and examples must be
adopter-neutral by default. Use synthetic project names, identifiers, paths
and scenarios. Do not publish private adopter prompts, logs, document bodies,
planning or roadmap material, credentials, local runtime state, or other
project-specific content. Do not present one adopter project's workflow as a
universal OrchestratorEngine contract.

Real project names and project-specific behavior are allowed only when the
user explicitly authorizes publication and the content is clearly identified
as an integration guide, compatibility profile or case study. Keep the
provider- and project-neutral contract in a separate canonical document.

Local artifact paths in reports are operator pointers, not evidence that a
maintainer can access those files. Prefer bounded diagnostics, sanitized
excerpts and minimal synthetic fixtures over complete logs or private data.

## Change scope

- Keep product-specific adapters and legacy bridges in adopting projects.
- Preserve existing user changes and durable audit artifacts.
- Add or update tests for behavioral changes.
- Do not commit or push unless the user explicitly authorizes it.

## Verification

Follow the [risk-based verification policy](docs/verification-policy.md). Use
structural checks for prose-only changes, focused tests for isolated behavior,
and one final full gate for shared contracts, packaging, CI, releases or broad
risk. Do not repeatedly run the complete suite between intermediate edits.

For setup, architecture and reporting details, see the
[setup guide](docs/setup-guide.md), [contracts](docs/contracts.md) and
[operator reporting policy](docs/operator-reporting.md).
