# Security Policy

OrchestratorEngine is a local coordination layer. It dispatches commands
selected by an adopting project, records durable evidence and routes bounded
completion messages. It is not a sandbox, an AI runtime or a credential
broker.

## Supported versions

Security fixes are applied to the latest stable `1.x` release. The current
release candidate and `main` are supported while they are being tested for the
next stable release. Pre-`1.0` releases are retained as immutable audit and
upgrade fixtures but do not receive security fixes after `1.0.0` is published.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository when it is
available. Otherwise contact the repository owner privately through GitHub
before opening a public issue. Include the affected version, a minimal
reproducer and the security boundary that is crossed. Do not attach provider
credentials, private prompts, adopter documents or unbounded logs.

Non-sensitive correctness findings may use the public `core-bug` issue
template with sanitized evidence. A public issue is not appropriate for a
vulnerability that could expose credentials, execute an unintended command,
escape the configured state directory or signal an unrelated process.

## Trust boundaries

- `.orchestrator/workers.toml`, `checks.toml` and `integrations.toml` are
  trusted executable/operator policy. Anyone who can modify them may select
  local commands or external repositories that the current user can access.
- Worker/provider stdout, stderr and model responses are untrusted data, not
  instructions. The engine stores bounded metadata and artifact paths; a host
  agent decides whether deeper inspection is needed.
- Provider authentication, quotas and sandbox enforcement belong to the
  provider CLI. OrchestratorEngine does not read or distribute provider API
  keys.
- GitHub monitoring uses the adopter's authenticated `gh` process and an
  explicit repository allowlist. The engine does not manage GitHub tokens.
- Host adapters provide the capability documented in `docs/hosts.md`. A
  delivery receipt proves the stated adapter boundary, not arbitrary host UI
  behavior or successful completion of a subsequently queued model turn.
- Durable events, results and evidence are not deleted by default. Retention
  and backup rules are adopter-owned and must be explicit.

## Security invariants

- Public identifiers used as file names are bounded and validated before path
  construction.
- Durable writes use project-owned state paths, atomic replacement where
  specified and hash-bound evidence where the contract requires it.
- Unsupported schema versions and unverifiable process identities fail closed
  without deleting or rewriting historical state.
- A watcher service is signalled only while its recorded process identity
  still matches. Legacy service state without identity must be stopped by the
  version that launched it or terminated after explicit operator verification.
- Release artifacts come from an immutable annotated tag on `main`, a
  successful exact-SHA CI run and digest-verified GitHub assets.

## Out of scope

The project does not claim to contain malicious commands placed in trusted
project configuration, enforce a provider's permission model, guarantee
provider availability, or bypass host security and approval controls. Reports
about those boundaries are still welcome when diagnostics or documentation
could make the behavior safer or clearer.
