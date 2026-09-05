# Compatibility Policy

Version `1.0.0` establishes the first stable public contract for
OrchestratorEngine. Compatibility applies to documented behavior, not to
private implementation details or adopter-owned policy text.

## Supported runtime scope

- Linux and WSL support the complete detached worker, monitor, check and
  watcher lifecycle.
- Native Windows and macOS support the portable core and compatible foreground
  checks. Unsupported detached commands fail before creating runtime state.
- Releases are distributed through immutable Git tags and digest-verified
  GitHub Release assets. Publication to PyPI is not part of the `1.0` contract.

Host delivery remains capability-based. A host may support live delivery,
durable non-live delivery or no callback channel without changing the core
event contract. The current capability matrix and receipt semantics are
defined in [Host adapters](hosts.md).

## Durable schema compatibility

The compatibility floor for schema-version-1 state is OrchestratorEngine
`0.10.0`. CI verifies upgrades from `0.10.0`, `0.11.1` and `0.12.0`; `0.13.0`
is the release-candidate reliability baseline.

Within the `1.x` line:

- readers accept documented required fields plus unknown optional properties;
- writers may add optional fields, new receipt kinds, new adapters and new
  commands without changing existing meaning;
- required fields, existing `kind` values, durable path layout and terminal
  status names remain compatible;
- unsupported future schema versions fail closed without deleting, replacing
  or silently acknowledging durable state.

A breaking durable-contract change requires a new schema version and, when it
changes documented `1.x` behavior, a new major package version.

## CLI and configuration compatibility

Documented commands, flags and TOML fields are stable for the `1.x` line.
Additive commands and optional fields are allowed. A deprecation must emit a
clear diagnostic, document the replacement and remain usable for the rest of
the `1.x` line. Removal or incompatible reinterpretation requires `2.0`.

Legacy aliases explicitly documented as compatibility shims follow the same
rule. Undocumented Python functions, internal state caches, test helpers and
generated command ordering are not public API.

Project-owned profile declarations and behavior policies describe operator
intent; they are not proof of model capability or sandbox enforcement. Core
remains provider-neutral and does not infer a model, effort level or permission
policy that the adopter did not declare.

## Upgrade procedure

Upgrade only from an immutable release tag. Run `doctor`, `upgrade check
--strict` and the adopter checklist before normal dispatch. Preserve durable
events, tasks, results, evidence and inbox signals while resolving findings.

Watcher services started before `1.0.0rc1` do not contain a process identity.
Stop them with the installed version that launched them before upgrading. If
the package was already upgraded, verify and terminate that process explicitly,
then start a new watcher service. The current CLI intentionally refuses to
signal an identity-less live PID.

See the [Upgrade Guide](upgrade-guide.md) and
[Adopter Upgrade Checklist](adopter-upgrade-checklist.md) for the complete
bounded workflow.
