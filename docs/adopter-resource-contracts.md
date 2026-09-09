# Adopter resource contract dispositions

Document ID: `OE-ADOPTER-RESOURCE-CONTRACTS`
Revision: `4`

This document records provider-neutral dispositions for adopter observations.
It intentionally excludes adopter repositories, private paths, product data and
host-specific resource semantics.

## OE-ADOPT-001: cancellation maintenance authority

Disposition: accepted as a blocking core defect.

An admitted owner now receives separate work and maintenance capabilities.
Cancellation revokes work authority but preserves maintenance authority for the
same stage and epoch while it remains the current running owner. The native
runner selects maintenance context only for registered cleanup and release
probes. Stale epochs, terminal stages, released owners and attempts to use a
maintenance token as work fail closed. Public status and outbox payloads omit
both tokens.

Revision 2 closes the legacy empty-token edge identified during adopter review.
Stored and supplied capabilities must both be nonempty strings before constant-
time comparison. A persisted stage from an earlier release receives no synthetic
maintenance credential: cleanup fails closed with an explicit drain-or-recover
diagnostic. The upgrade contract therefore requires draining resource stages
before installing this capability model.

Revision 3 adds the literal legacy-state regression: a real running stage with
no stored maintenance capability rejects both an omitted and an empty supplied
token through `Authority.call`. This test fails against the pre-fix comparison
and complements the runner-side legacy recovery test.

## OE-ADOPT-002: bind and dispatch race

Disposition: accepted as an integration race with a core dispatch remedy.

Worker and check dispatch accept a validated `--wake-target-file`. When present,
the exact operation-scoped snapshot is persisted without consulting mutable
project binding. Existing binding capture remains compatible for single-owner
flows. Adopters that cannot provide an operation snapshot should serialize
binding mutation and dispatch locally.

## OE-ADOPT-003: authority enrollment lifecycle

Disposition: accepted with a conservative drained-update contract.

`resource update` performs a revision-checked update while holding the service
instance lock. The authority must be stopped and have no nonterminal stages.
Authority identity and existing project credentials are preserved; adding
projects and changing recipes or drained resource definitions is supported.
Removing projects and changing existing roots are rejected because durable
requests and result delivery may still refer to them.

The contract does not infer resource quiescence, rewrite active attempts or
provide rolling live-service reconfiguration. Those remain explicit future
extensions if adopter evidence justifies them.

## OE-ADOPT-005: retained input submission

Disposition: accepted as a public CLI/API integration gap.

`resource create-input-contract` records the external request ID, registered
recipe digest, lineage and exact input SHA-256 manifest before a delayed
launcher can outlive its selection lock. `resource submit --input-contract`
then submits only that retained contract. A new request still requires the
registered live root to match and is captured again by the authority. An
already accepted identical request replays before live files are read; a
changed contract under the same request ID conflicts.

This closes the orphan-launcher misassociation window without granting new
authority, accepting arbitrary commands or moving adopter resource semantics
into the core. The public schema is `resource-input-contract` version 1.
