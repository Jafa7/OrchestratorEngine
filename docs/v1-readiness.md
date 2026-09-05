# Version 1 Readiness

Version `1.0` is a stability boundary, not a request for more orchestration
features. The existing provider-neutral core must satisfy the criteria below
before the release tag is created.

## Supported runtime scope

- Linux and WSL provide the complete detached worker, monitor and watcher
  lifecycle.
- Native Windows and macOS provide the portable core and compatible foreground
  checks. Unsupported detached operations fail before changing runtime state.
- Host delivery remains capability-based: a host may provide live delivery,
  durable non-live delivery or no callback channel without changing core event
  contracts.

Native detached lifecycle support is not a `1.0` requirement while this
boundary remains explicit in the platform report and documentation.

## Release gates

1. Public CLI, TOML and durable JSON contracts have documented compatibility
   and deprecation rules for the `1.x` line.
2. Every packaged schema has valid and invalid fixtures, and unknown future
   schema versions fail closed without deleting durable state.
3. Historical wheel upgrades from the declared compatibility floor pass in CI
   without `PYTHONPATH`.
4. Repeated installed-wheel full conformance passes, including concurrent
   dispatch, host routing, recovery and idempotent delivery.
5. A repository security review covers command execution boundaries, untrusted
   worker output, path handling, credentials and release provenance.
6. Setup, upgrade, platform, external-tool and troubleshooting documentation
   agree with the shipped behavior.
7. One release candidate completes its local gate, exact-SHA CI and GitHub
   artifact verification without unresolved critical findings.

The release does not require PyPI publication, autonomous merge/push behavior,
project roadmap interpretation or provider-specific policy in core. GitHub
Release assets and immutable annotated tags remain a supported installation
channel.
