# Upgrade Guide

This guide covers OrchestratorEngine runtime state upgrades. It is separate
from project-specific legacy bridge work, which belongs in adopting projects.

## Version Check

Check the installed CLI version:

```bash
orchestrator-engine --version
```

The current release is `0.1.1` and the durable JSON contract schema version is
`1`.

Upgrade from the immutable Git tag (the package is not currently published to
PyPI):

```bash
python -m pip install --upgrade \
  "orchestrator-engine @ git+https://github.com/Jafa7/OrchestratorEngine.git@v0.1.1"
```

## Schema Compatibility

Every durable JSON contract includes `schema_version`. OrchestratorEngine v0.1
accepts schema version `1`.

Before and after an engine upgrade, run:

```bash
orchestrator-engine --project-root /path/to/project doctor
```

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
