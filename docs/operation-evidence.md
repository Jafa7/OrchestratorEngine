# Operation Evidence

`operation evidence` is an exact-target, read-only metadata query for integrations
that need retained local-check lineage without importing private modules or
reading command output. It does not dispatch, repair, initialize a database,
acknowledge results, or decide product acceptance.

```bash
orchestrator-engine --project-root /path/to/project operation evidence \
  --target check:CHECK-001
```

The default is the compatibility contract v1. An opt-in v2 envelope adds a
producer-owned applicability declaration for local checks that were dispatched
with one:

```bash
orchestrator-engine --project-root /path/to/project operation evidence \
  --target check:CHECK-001 --contract-version 2
```

The v2 envelope embeds the complete v1 terminal-evidence report and adds a
separately validated applicability graph. It does not reinterpret or strengthen
the v1 result. Its closed response schema is
[`operation-evidence-v2.json`](../src/orchestrator_engine/schemas/operation-evidence-v2.json).

## Applicability Declaration

An ordinary local check may retain a bounded declaration before launch:

```bash
orchestrator-engine --project-root /path/to/project check run \
  --check-id CHECK-001 --suite full --execution auto --wake-policy auto \
  --applicability-input /path/to/applicability.json
```

```json
{
  "schema_version": 1,
  "kind": "ORCHESTRATOR_OPERATION_APPLICABILITY_DECLARATION",
  "project": "sample-project",
  "work": "sample-work",
  "revision": "revision-4",
  "candidate": "0123456789abcdef",
  "criteria": [{"id": "criterion-1", "revision": "revision-2"}],
  "retry_of": null
}
```

The declaration is inert data: identifiers do not grant authority, fetch a URI,
select files or alter the configured suite. Input is strict bounded UTF-8 JSON.
Duplicate keys, non-integer schema versions, non-finite or ambiguous numeric
forms, duplicate criteria, symlinks and oversized input fail before descriptor
publication. Resource-managed checks and other producer kinds do not support
this opt-in and reject it before submission.

Admission retains the original bytes once and publishes a canonical declaration
digest, a raw-byte digest, a producer-assigned attempt UUID, the suite
fingerprint and a versioned native source/location binding. Same-ID semantic
replay reuses the retained attempt. Any declaration or retry change conflicts;
terminal operations cannot be enriched later. An interrupted one-file
preparation has no execution authority and requires explicit recovery.

An exact retry names one supported terminal predecessor by operation ID,
attempt UUID, applicability digest and source/location binding. Project and
logical work declarations must match; candidate, revision and criteria may
change. There is no global lineage scan or inference from timestamps.

The applicability metadata is copied into result and evidence before the
terminal event hashes them. Foreground and detached supervisors revalidate the
retained input, artifact, descriptor, source and suite binding before the first
command. The v2 reader then reports `retained`, `unsupported`, `unknown` or
`conflicted`, and fences the descriptor, applicability files and terminal
artifacts with one bounded reread.

`retained` means only that the caller declaration was preserved and bound to
the producer attempt. It does **not** prove that the declared candidate equals
the source bytes used by commands, that a mutable checkout stayed unchanged, or
that declared criteria were fulfilled. Those assurance fields remain
`unknown`. Exact executed-candidate proof requires a separate verified snapshot
or execution boundary supplied by an explicit adapter or adopter policy.

The native location digest hides literal paths from the response but is neither
anonymous nor a portable project identity. Moving the project or state root
causes a source mismatch; historical files are not rewritten. `complete`
describes complete observation of this bounded graph, not product acceptance.

## Supported Producer

The initial implementation supports native `ORCHESTRATOR_LOCAL_CHECK` artifacts
with schema version 1. Syntax also accepts `worker:ID`, `ci:ID`, and `pr:ID`;
these return `completeness: unsupported` and an empty artifacts array without
reading those producers. The requested kind is preserved, not rewritten as
`check`. A different producer in the shared check namespace is unsupported when
its supported descriptor or retained operation-owner record identifies it.
The optional owner record is checked even when a local descriptor exists.
A supported owner for another producer or another operation yields unsupported
coverage; a conflicting local graph also carries blocking `identity_mismatch`.
An unreadable, malformed or future-version owner blocks complete coverage.
Truly absent legacy ownership is allowed without inventing an owner or writing
one; its absence is checked again at the snapshot fence.
Missing native state returns `unavailable`, not an invented successful check.
IDs use ASCII `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`.

The closed v1 response schema is packaged as
[`operation-evidence.json`](../src/orchestrator_engine/schemas/operation-evidence.json).
The initial native producer has no proved candidate or retry identity, so
`candidate` and `attempt` are explicitly unknown with null values. Generic
fingerprints, directory names and Git state are not candidate provenance.
`source.project_id` comes only from a validated retained terminal event;
absence stays unknown. A consumer registration qualifies that non-global
namespace but cannot replace missing native identity evidence.

## Artifact Bindings

Supported queries always include exactly three roles, in this order:
`result`, `evidence`, `event`. An artifact contains its presence, observed raw
SHA-256 and byte size (or null), retained expected digest bindings, and integrity.
No body, logs, argv, prompts, absolute paths or exception text are returned.

Result expectations may come from both `check_evidence` and `terminal_event`.
Evidence expectations come from the terminal event. Every applicable supported
binding is checked; one matching binding cannot override a conflicting one.
Bindings are unique by basis and sorted by basis. Expected hashes from other
readable validated bindings are preserved even when current artifact bytes
are missing, unreadable or budget-limited. In that case `observed` is null and
integrity is `unavailable`, not `matched` or `mismatch`.

The root event has no ancestor digest in this producer. Its integrity normally
remains `observed_only`; this does not invalidate supported outgoing bindings.
`observed_only` result or evidence is insufficient for retained-lineage acceptance.
Readable bytes are not automatically valid schema, identity or lineage evidence.

## Snapshot Fence

The optional ownership file is read initially and reread at the final fence,
including an initially absent file. Creation, disappearance or replacement
invalidates a stable native snapshot. All its reads share the cumulative
allowance; it is not a fourth response artifact.

The query reads canonical `checks/ID/check.json` first. A supported terminal
descriptor must already carry its publication references. Publication order is
result/evidence, terminal event, then final descriptor. An event observed while
the descriptor is still active yields `unsealed`/`publication_incomplete`.

Paths resolve inside the configured state root; escaped and outward symlink
paths are not read. After bounded artifact reads, the exact descriptor and
event are reread once and their complete raw bytes compared. A changed or lost
fence yields `changed`/`snapshot_changed`; there is no retry loop. Missing
supported final fencing remains unknown. Stable means only this observation
was consistent, not that files cannot change later. A coherent malicious
replacement of the entire graph is outside this local snapshot guarantee.

## Bounds And Diagnostics

The cumulative raw-read allowance is 8 MiB, including descriptor/event rereads
and a one-byte over-limit sentinel. Each read requests at most the remaining
allowance plus that sentinel; oversized bytes are not parsed or exported.
The envelope is limited to 64 KiB UTF-8. Initial CLI limits cannot be increased.
Project identity is at most 256 UTF-8 bytes, engine version at most 64 bytes,
and UTC RFC3339 timestamps at most 40 bytes. JSON Schema character lengths do
not replace these byte constraints. Values are rejected, never silently trimmed
or truncated. Duplicate JSON keys and non-JSON numeric constants are invalid.

`errors` and `omissions` each contain at most 16 unique fixed-code entries,
sorted by code and then role. `diagnostics_truncated` is explicit; its marker
counts inside the 16-entry allowance. The schema enumerates all supported codes.
There are no per-artifact schema/identity-state fields: attributed errors report
detected invalid schema or identity, while omissions and partial coverage report
validation that could not be established. Unknown codes are not ignorable.

## Consumer Eligibility

The pinned supported schema projection checks every required top-level field
and every present declared top-level type, enum, scalar pattern, numeric bound
and timestamp in the packaged native descriptor, result, evidence, event and
owner schemas. Unknown versions/kinds are unsupported. Missing or malformed
required schema coverage emits `unsupported_schema` in both errors and omissions
and prevents complete coverage. Absolute path patterns use the current native
platform rather than legacy POSIX-only evidence patterns. Digest fields must
be exactly 64 lowercase hexadecimal characters.

This is deliberately not a general JSON Schema validator: nested command
bodies, free-form plan/process objects, transport-reference contents and unused
extra producer fields are not execution or acceptance evidence. Their declared
top-level container types are checked; their contents are not exported or
validated as product criteria. Separate identity/link checks still apply to
the fields this query uses. Envelope validation uses the closed public schema.

`completeness` measures coverage, not correctness. An examined digest mismatch
can coexist with complete coverage and must still fail a consumer gate. Missing,
unreadable, unsupported or unexamined required schema/identity/link coverage
cannot establish eligible complete lineage.

For retained local lineage, require all of the following together:

- supported envelope and native producer semantics, exact target and native
  project identity;
- supported terminal descriptor, stable snapshot, complete coverage and all
  three required roles;
- no blocking errors, omissions, unknown codes or truncated diagnostics;
- valid event identity and outgoing links, with result/evidence matched to
  **all** required bindings (root-event `observed_only` is allowed).

Absence of errors alone is not validation evidence. Unknown native project
identity cannot satisfy exact-project acceptance. Candidate unknown cannot
satisfy exact-candidate applicability even when historical lineage is intact.
Historical execution evidence never establishes current criteria, review
quality or product acceptance.

Exit `0` means a structured domain report, including missing, unknown, mismatch
and unsupported outcomes. Exit `2` means invalid command/target syntax. Exit `1`
means an execution failure prevented a domain report. Never use exit `0` as an
acceptance shortcut.
