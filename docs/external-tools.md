# External tool prerequisites

OrchestratorEngine has no runtime Python dependencies, does not call provider
APIs directly and does not install or authenticate third-party CLIs. Optional
features execute explicitly configured local tools through argv without a
shell. The adopter owns installation, updates, authentication and local
policy for those tools.

| Feature | External tool | Required | Verify |
| --- | --- | --- | --- |
| Core files, schemas and status | none | always available | `orchestrator-engine --version` |
| Local check runtime | adopter-declared commands | only for each configured suite | Run each command's native `--version` or equivalent |
| Codex worker or live host queue | Codex CLI | only for Codex profiles/host | `codex --version`; `codex queue --help` for live delivery |
| Claude worker | Claude Code CLI | only for Claude profiles | `claude --version` |
| Copilot worker | GitHub Copilot CLI | only for Copilot profiles | `copilot --version` |
| VS Code host callback | Visual Studio Code CLI | only for the VS Code host | `code --version` |
| GitHub Actions monitor | GitHub CLI (`gh`) | only for `ci watch` | `gh --version`; `gh auth status --hostname github.com` |

Install external tools from their official documentation. For GitHub CLI use
the official [installation guide](https://github.com/cli/cli#installation)
and [authentication guide](https://cli.github.com/manual/gh_auth_login).
OrchestratorEngine never requests, prints or stores a GitHub token. A missing,
unauthenticated or incompatible executable is reported as an integration
failure, not silently installed or repaired.

Machine-specific paths belong in ignored adopter-local configuration such as
`.orchestrator/workers.toml`, `.orchestrator/checks.toml` or
`.orchestrator/integrations.toml`. Local check commands are project-owned
prerequisites; the engine executes their argv but does not install them. Do not put
private executable paths, credentials or host backup policy in the public
OrchestratorEngine repository.
