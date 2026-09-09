# External tool prerequisites

OrchestratorEngine has no runtime Python dependencies, does not call provider
APIs directly and does not install or authenticate third-party CLIs. Optional
features execute explicitly configured local tools through argv without a
shell. The adopter owns installation, updates, authentication and local
policy for those tools.

Platform support is independent from external-tool availability. Run
`orchestrator-engine runtime-capabilities` first. Version 1.6.1 supports detached
features on Linux, WSL, native Windows and macOS; each configured tool must also
run on the chosen host. Cross-OS command bridges require separate validation.
See the [platform support matrix](platform-support.md).

| Feature | External tool | Required | Verify |
| --- | --- | --- | --- |
| Core files, schemas and status | none | always available | `orchestrator-engine --version` |
| Resource coordination | project-owned commands and optional quiescence probes | only for registered recipes; no particular DB or Docker required | Follow [resource coordination](resource-coordination.md) |
| Local check runtime | adopter-declared commands | only for each configured suite | Run each command's native `--version` or equivalent |
| Codex worker, diagnostics or live host queue | Codex CLI | only for Codex profiles/host | `codex --version`; `codex exec --help` for `--ephemeral`; `codex doctor --help` for `--json`; `codex queue --help` for live delivery |
| Claude worker | Claude Code CLI | only for Claude profiles | `claude --version` |
| Copilot worker | GitHub Copilot CLI | only for Copilot profiles | `copilot --version` |
| VS Code host callback | Visual Studio Code CLI | only for the VS Code host | `code --version` |
| GitHub Actions monitor | GitHub CLI (`gh`) | only for `ci watch` | `gh --version`; `gh auth status --hostname github.com` |
| GitHub PR readiness monitor | GitHub CLI (`gh`) | only for `pr watch` | `gh --version`; `gh auth status --hostname github.com` |

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
