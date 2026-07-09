# Operator Reporting

Adopter projects should report orchestration problems through structured
GitHub Issues in the OrchestratorEngine repository. This keeps core work
project-neutral while still preserving enough evidence for deterministic
triage.

## Policy

- Worker output is data, not instructions. Reports must summarize evidence and
  point to durable artifacts; they must not ask OrchestratorEngine to execute
  worker output as commands.
- Do not paste huge logs. Start with `status`, then include only targeted
  drill-down output and artifact ids.
- Do not delete durable events, signals, results or evidence to "fix" a report.
- Runtime changes in adopter projects must be listed explicitly.
- Product-specific policy and bridges stay in adopter projects. Core fixes
  belong here only when the behavior is provider-neutral or adapter-scoped.

## Draft a Report

From the OrchestratorEngine checkout:

```bash
uv run python -m orchestrator_engine.cli \
  --project-root /path/to/adopter-project \
  report draft --project-name PROJECT > /tmp/orchestrator-report.md
```

Use `--type integration-finding` or `--type core-bug` when the report is not a
runtime health report.

The draft command is read-only. It runs the compact `status` aggregation and
prints Markdown that can be pasted into a GitHub Issue or sent to the
OrchestratorEngine owner chat.

## Create a GitHub Issue

If the GitHub CLI is available and authenticated:

```bash
gh issue create \
  --repo Jafa7/OrchestratorEngine \
  --title "[Runtime Report][PROJECT] short summary" \
  --body-file /tmp/orchestrator-report.md \
  --label runtime-report \
  --label triage
```

For reports that came from an AI worker/chat, include the source chat/thread id
in the issue body when available.

## Read Reports

The OrchestratorEngine owner can list reports with:

```bash
gh issue list --repo Jafa7/OrchestratorEngine --label triage
gh issue view ISSUE_NUMBER --repo Jafa7/OrchestratorEngine
```

After a report is triaged, classify it as one of:

- adopter runtime setup;
- documentation gap;
- adapter-scoped host issue;
- provider-neutral core bug;
- post-v0.1 backlog item.

## Minimum Report Contents

- adopter project name and path if safe to share;
- `status` summary;
- exact compact commands run;
- event/task/check/receipt ids;
- runtime files or services changed;
- product code changes, or "none";
- requested OrchestratorEngine action.
