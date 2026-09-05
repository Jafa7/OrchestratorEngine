# Heterogeneous Operation Wait

`operation status` reads one immediate snapshot and `operation wait` blocks one
local process on a bounded set of existing OrchestratorEngine operations. Both
commands read bounded local descriptor, lease and process-identity state only;
they do not call a model, query GitHub, read worker output or inspect full logs.

Use `status` in scripts, dashboards or operator checks that must not block:

```bash
orchestrator-engine --project-root /path/to/project operation status \
  --target worker:IMPLEMENT-1 \
  --target check:FINAL-GATE-1 \
  --mode all
```

An active, readable status returns exit code `0`. A terminal unsuccessful set
returns `2`, and lifecycle damage that needs attention returns `3`.

Use `wait` when one active host turn needs to block on a mix of operations:

```bash
orchestrator-engine --project-root /path/to/project operation wait \
  --target worker:IMPLEMENT-1 \
  --target check:FINAL-GATE-1 \
  --target ci:gha-sha-0123456789ab-example \
  --target pr:pr-42-example \
  --mode all
```

Supported target kinds are `worker`, `check`, `ci` and `pr`. Repeat `--target`
for up to 64 unique `KIND:ID` values. `--mode all` returns when every target is
terminal; `--mode any` returns when the first target is terminal. Lifecycle
damage such as an invalid descriptor or a crashed/stalled supervisor takes
priority and returns immediately as `action_required`.

Exit codes are stable:

| Code | Meaning |
| ---: | --- |
| `0` | The selected condition completed successfully. |
| `2` | The selected condition is terminal but at least one relevant result is unsuccessful. |
| `3` | At least one operation needs operator or agent attention. |
| `124` | The optional timeout elapsed while the condition remained active. |

`operation status` always prints one bounded
`ORCHESTRATOR_OPERATION_WAIT_STATUS` object. Use `operation wait --json` for the
same final machine-readable shape after blocking. The object contains statuses
and artifact paths, never command output or log tails. Interactive wait output
uses one compact colored line and an optional terminal bell, matching `worker
wait` behavior.

This command is complementary to watcher delivery. For short waits inside an
active turn, one blocking wait avoids model polling and preserves the current
agent context. Dispatch those operations with `--wake-policy never`. For long
work, enable `always`, `on-failure` or the operation's equivalent policy, end
the turn, and let the host-specific watcher wake the dispatching chat after
terminal evidence is written. Do not use both routes for the same operation:
the watcher may queue its message while the blocking wait is already handling
the result.

The bounded status object includes `wakeup_enabled_targets` and
`duplicate_followup_risk`. These are diagnostics, not delivery controls: a
signal already submitted to a host queue cannot be recalled. For a multi-stage
pipeline, use `never` for intermediate checks and monitors; only the terminal
handoff boundary should create a wakeup.

Worker tasks support the same choice:

```bash
orchestrator-engine --project-root /path/to/project worker run \
  --worker cheap --task-id IMPLEMENT-1 --prompt-file /path/to/prompt.md \
  --wake-policy never
orchestrator-engine --project-root /path/to/project operation wait \
  --target worker:IMPLEMENT-1 --json
```
