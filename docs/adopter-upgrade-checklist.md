# Adopter Upgrade Checklist

Use this checklist after installing a newer OrchestratorEngine release in an
adopting project. It is intentionally project-neutral: the adopter owns its
worker commands, policies, verification commands and host channel.

## 1. Install an immutable release

Choose a published tag from the OrchestratorEngine GitHub Releases page. Do
not install a moving branch for normal project operation.

```bash
python -m pip install --upgrade \
  "orchestrator-engine @ git+https://github.com/Jafa7/OrchestratorEngine.git@vX.Y.Z"
orchestrator-engine --version
```

Record the installed version in the adopter handoff. The engine does not make
a provider/API call to discover a newer version.

## 2. Run bounded diagnostics

```bash
orchestrator-engine --project-root /path/to/project doctor
orchestrator-engine --project-root /path/to/project worker diagnose --enabled-only
orchestrator-engine --project-root /path/to/project upgrade check --strict
```

`upgrade check` is read-only. It summarizes engine/schema compatibility,
doctor results, enabled profiles, dispatch settings and bundled-policy drift.
It also returns a manual audit list because no deterministic command can know
whether prose instructions still reflect the user's current intent.

Fix `blocked` findings before dispatch. Review warnings deliberately; do not
delete `.orchestrator/events`, `.orchestrator/tasks` or inbox signals to make
the report clean.

## 3. Compare, do not overwrite, worker policy

Existing adopter policy may be intentionally customized. Export the installed
reference to a temporary path and compare it:

```bash
orchestrator-engine worker policy export \
  --name quality-efficient \
  --output /tmp/orchestrator-quality-efficient.md
diff -u .orchestrator/policies/quality-efficient.md \
  /tmp/orchestrator-quality-efficient.md
```

Review and merge useful changes into the adopter copy. The export command
refuses to overwrite its destination unless `--replace` is explicit, and no
upgrade command overwrites the adopter policy automatically.

## 4. Audit instructions used for future tasks

Inspect the adopter's `AGENTS.md`, `CLAUDE.md`, Copilot instructions and
reusable prompt templates. Replace stale unconditional commands such as
"always run the full suite" with the current risk-based policy:

- structural checks for prose-only work with no runtime, contract or generated
  effect;
- focused owning-module checks for isolated behavior;
- one final full gate for shared contracts, CLI, packaging, CI, releases or
  remaining uncertainty.

Do not rewrite historical `.orchestrator/tasks/*/effective-prompt.md`, events,
results or evidence. They are immutable audit records of past dispatches.

For each new AI task, create a `WORKER_TASK_INTENT` JSON document and select
the verification breadth before dispatch:

```json
{
  "role": "implementation",
  "risk": "low",
  "verification": "structural",
  "permissions": "restricted",
  "authorizations": {
    "commit": false,
    "push": false,
    "network": false
  }
}
```

The generated effective prompt declares `intent.verification` authoritative.
Generic or copied task text cannot broaden it. If a current explicit user
request conflicts with the intent, stop and dispatch a corrected intent; do
not silently choose either instruction.

Also audit future-facing public documentation, fixtures and examples for
adopter neutrality. Use synthetic names, identifiers, paths and scenarios by
default. Real adopter details require explicit publication authorization and a
clearly labeled integration guide, compatibility profile or case study. Do not
rewrite historical durable artifacts during this audit.

## 5. Smoke the new dispatch contract

Bind the current host chat, then dispatch one harmless bounded task with
`--intent-file`. Inspect these files before enabling normal workloads:

- `.orchestrator/tasks/TASK/task.json`;
- `.orchestrator/tasks/TASK/effective-prompt.md`;
- `.orchestrator/tasks/TASK/evidence.json`;
- the terminal event and inbox signal.

Confirm the effective prompt contains the selected policy and authoritative
verification level, the hashes agree, and the completion routes to the chat
that dispatched the task. Restart the callback watcher or re-arm Claude stream
delivery when the installed process still runs the old executable.

## Agent Handoff Prompt

The repository owner can give an adopter agent this bounded request:

```text
Upgrade this project to the explicitly selected OrchestratorEngine release.
Follow docs/adopter-upgrade-checklist.md from that immutable release.
Preserve durable .orchestrator events/tasks/results/evidence and all local
policy customizations. Run doctor, worker diagnose and upgrade check; compare
the bundled quality-efficient policy through worker policy export. Audit only
future-facing agent instructions and reusable prompt templates for stale
unconditional verification commands and public-content rules that expose or
universalize one adopter's private workflow. Dispatch one harmless task with
an explicit intent.verification and inspect its effective prompt/evidence.
Report compact findings and exact commands. Do not commit or push unless the
user explicitly authorizes it.
```
