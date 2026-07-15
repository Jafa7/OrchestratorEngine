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
- Treat local artifact paths as operator pointers, not as files a maintainer
  can access. Include sanitized excerpts or a synthetic fixture when the
  content itself is required to reproduce a core bug.
- Omit private document bodies, private planning content, credentials and
  unbounded generated context. Sanitize project configuration excerpts.
- Treat `effective-prompt.md` as private durable task evidence. Report its path,
  size or hash when useful, but do not paste its policy/task body into a public
  issue.
- If `worker tasks` reports `task_large_worker_log`, include the task id,
  affected log artifact names and sizes, but keep log bodies out of the issue.
- Do not delete durable events, signals, results or evidence to "fix" a report.
- If an unsuccessful historical task has already been handled, use
  `worker resolve` to record an operator resolution instead of deleting the
  task directory or hiding the audit trail.
- Runtime changes in adopter projects must be listed explicitly.
- Product-specific policy and bridges stay in adopter projects. Core fixes
  belong here only when the behavior is provider-neutral or adapter-scoped.

## Draft a Report

From an environment where OrchestratorEngine is installed:

```bash
orchestrator-engine --project-root /path/to/adopter-project \
  report draft --project-name PROJECT > /tmp/orchestrator-report.md
```

Use `--type integration-finding` or `--type core-bug` when the report is not a
runtime health report.

The draft command is read-only. It runs the compact `status` aggregation and
prints Markdown that can be pasted into a GitHub Issue or sent to the
OrchestratorEngine owner chat. It omits the absolute project root by default;
add a sanitized path manually only when it is useful for triage.

## Create a GitHub Issue

If the GitHub CLI is available and authenticated:

```bash
gh issue create \
  --repo Jafa7/OrchestratorEngine \
  --title "[Runtime Report][PROJECT] short summary" \
  --body-file /tmp/orchestrator-report.md \
  --label runtime-report \
  --label triage \
  --label project:PROJECT \
  --label source:HOST
```

For reports that came from an AI worker/chat, include the source host. Include
the local chat/thread id only when it is useful and safe to share.

Issue authorship usually shows the GitHub account or token that created the
issue. Treat author as transport identity only. The report source is encoded in
the title, body and labels.

Recommended labels:

- report class: `runtime-report`, `integration-finding` or `core-bug`;
- lifecycle: `triage`;
- adopter project: `project:example-project`;
- source host: `source:codex`, `source:claude`, `source:vscode`.

## Read Reports

The OrchestratorEngine owner can list reports with:

```bash
gh issue list --repo Jafa7/OrchestratorEngine --label triage
gh issue list --repo Jafa7/OrchestratorEngine --label project:example-project
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
- privacy notes describing anything omitted or sanitized;
- requested OrchestratorEngine action.
