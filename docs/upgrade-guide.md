# Upgrade Guide

This guide covers OrchestratorEngine runtime state upgrades. It is separate
from project-specific legacy bridge work, which belongs in adopting projects.

## Version Check

Check the installed CLI version:

```bash
orchestrator-engine --version
```

The current release is `0.5.1` and the durable JSON contract schema version is
`1`.

Upgrade from the immutable Git tag (the package is not currently published to
PyPI):

```bash
python -m pip install --upgrade \
  "orchestrator-engine @ git+https://github.com/Jafa7/OrchestratorEngine.git@v0.5.1"
```

## Schema Compatibility

Every durable JSON contract includes `schema_version`. OrchestratorEngine v0.1
accepts schema version `1`.

Before and after an engine upgrade, run:

```bash
orchestrator-engine --project-root /path/to/project doctor
orchestrator-engine --project-root /path/to/project upgrade check --strict
```

For the complete adopter procedure, including local-policy comparison,
future-facing instruction audit and a dispatch smoke, follow the
[adopter upgrade checklist](adopter-upgrade-checklist.md).

The `schema_compatibility` check surveys durable events, inbox operational
JSON, bindings and worker task descriptors without rewriting or deleting them.
It reports unsupported schema versions and unreadable JSON as
operator-visible findings.

If a command reports an unsupported schema:

1. Stop any watcher service for the project.
2. Keep `.orchestrator/events`, `.orchestrator/tasks` and
   `.orchestrator/inbox/signals`; do not delete durable audit artifacts.
3. Check the engine version with `orchestrator-engine --version`.
4. Upgrade or downgrade OrchestratorEngine so the installed engine supports the
   state schema.
5. Restart the watcher or re-arm the stream watch.

Manual deletion of durable events, task results or evidence is not a supported
upgrade path.

## Worker policy adoption after v0.1.1

Worker behavior policies are additive. Existing `workers.toml` profiles with
no `policy` continue to dispatch and receive an informational
`worker_policy_not_configured` diagnostic; no existing task artifact needs to
be rewritten.

For an existing adopter:

1. Update the engine, then run `adopt` again. It creates the missing
   `.orchestrator/policies/quality-efficient.md` file without overwriting
   `workers.toml` or existing policy files.
2. Add a `[policies.quality-efficient]` table to `workers.toml` and assign
   `policy = "quality-efficient"` only to the intended AI profiles.
3. Run `worker list` and `worker diagnose --enabled-only`.
4. Dispatch a harmless new task and verify its `task.json`,
   `effective-prompt.md` and `evidence.json` hashes.

Newly dispatched tasks always receive an immutable `effective-prompt.md` task
snapshot. A selected policy is prepended to that snapshot. Old task
directories remain valid without the new optional fields, and schema version
stays at `1` because this is a forward-compatible addition.

`adopt` never overwrites an existing project-local policy. Export the bundled
reference with `worker policy export --name quality-efficient --output PATH`,
compare it with the adopter's `.orchestrator/policies/quality-efficient.md`,
review the change, and update the adopter copy explicitly. The current policy
keeps implementation context
through final risk-selected verification, uses one blocking deterministic
check-runner call for long gates, and forbids using another AI merely to poll
or wait for that process.

Policy revision 2 also makes `WORKER_TASK_INTENT.verification` authoritative
for the dispatched task. Copied or reusable task prose cannot silently broaden
that level. Strict AI profiles without an admission `verification` declaration
receive a diagnostic so adopter upgrades cannot appear fully configured while
leaving the decision ambiguous.

## Dispatch admission after v0.2.0

Availability and intent admission are opt-in. Existing configurations retain
their behavior: availability defaults to `off`, and legacy
`enforce_intent = true` still performs permission-only enforcement.

To require a positive adopter-owned availability probe, configure
`[dispatch].availability_mode = "require-available"`. To validate all fields
of `WORKER_TASK_INTENT`, configure `intent_enforcement = "strict"` and add a
`[workers.NAME.admission]` block. Do not set `enforce_intent` and
`intent_enforcement` together.

## Handoffs and completed-task acknowledgements after v0.3.1

New dispatches include a complete schema-valid `WORKER_HANDOFF` example in the
effective prompt. Existing task artifacts are immutable and do not need to be
rewritten; a malformed historical optional handoff remains evidence of that
worker run.

Completed tasks can now record a durable acknowledgement for a specific
non-error diagnostic. Use `worker resolve --status acknowledged` with one or
more repeated `--diagnostic-code CODE` options after verifying the real durable
output. Matching warnings remain visible as `info`; errors are never
downgraded. Existing unsuccessful-task resolutions remain compatible and do
not require diagnostic codes.

## Historical artifact lifecycle after v0.3.2

If a pre-v0.3.2 worker followed the old generated handoff prompt and omitted
`schema_version`, inspect the original and run `artifact resolve --path PATH
--reason TEXT`. The engine writes an immutable companion record bound to both
the state-relative path and exact SHA-256. It never edits the handoff. The
acknowledgement stops applying if the bytes change and cannot acknowledge
unreadable JSON or integer unsupported schema versions.

Existing `superseded` task resolutions may add `diagnostic_codes` while keeping
the same top-level status and `superseded_by_task_id`. Repeat those fields with
`worker resolve --replace`; matching historical warning/info diagnostics no
longer affect normal aggregate health, but remain available at info severity.
After writing such a resolution, do not roll that state directory back to
v0.3.2: its Python validator rejected `diagnostic_codes` on `superseded`
records even though the packaged schema allowed the field. Upgrade forward or
remove the added codes explicitly before running the older engine.

## Watcher State

Watcher state files are operational delivery state, not the source audit
record. They can be regenerated from inbox signals when needed, but doing so
may re-deliver old signals unless seen event ids are preserved.

When moving from an unscoped callback watcher to host-scoped callback watchers,
the new host-specific state files are seeded from the legacy
`watcher-state.json` seen ids on first use. This prevents duplicate deliveries
for events already handled by the legacy watcher.

## v0.1 Operator Commands

Show the compact aggregate operator report:

```bash
orchestrator-engine --project-root /path/to/project status
```

List deferred events:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host HOST deferred list
```

Retry a deferred event after fixing the delivery channel or quota state:

```bash
orchestrator-engine --project-root /path/to/project watcher \
  --host HOST deferred retry --event-id EVENT_ID --reason "quota reset"
```

Acknowledge an event already handled manually:

```bash
orchestrator-engine --project-root /path/to/project watcher --host HOST \
  acknowledge --event-id EVENT_ID --reason "read manually"
```

For host-scoped callback services, pass the same `--host HOST` used by the
service, or pass the exact `--state-file`, so operator commands read the
host-specific watcher state.
