# Native Windows and macOS Acceptance

OrchestratorEngine separates native runtime evidence from interactive desktop
behavior. GitHub Actions verifies the deterministic CLI and process lifecycle
from the candidate wheel on Windows x64, macOS Apple Silicon and macOS Intel. A
machine owner verifies Desktop and environment behavior because hosted CI has
no normal logged-in graphical session.

## Automated acceptance

From a checkout of the exact candidate tag, install that candidate in an
isolated environment. On macOS, run:

```bash
python tools/run_native_acceptance.py \
  --cli "$(command -v orchestrator-engine)" \
  --iterations 5 --timeout-seconds 15 \
  --expected-system Darwin --expected-machine "$(uname -m)" \
  --output native-acceptance.json
```

On Windows PowerShell, with the active environment containing the installed
candidate, run:

```powershell
python tools/run_native_acceptance.py `
  --cli (Get-Command orchestrator-engine).Source `
  --iterations 5 --timeout-seconds 15 `
  --expected-system Windows --expected-machine $env:PROCESSOR_ARCHITECTURE `
  --output native-acceptance.json
```

Machine names are normalized between `AMD64`/`x86_64` and
`ARM64`/`aarch64`. The command fails on another operating system, architecture
mismatch, an unavailable installed CLI, malformed capability output,
unsupported lifecycle capabilities or any failed soak iteration.

The hosted baseline is Windows Server 2022 x64 and macOS 15 on Intel and Apple
Silicon. Windows 10/11, Windows ARM64, other macOS releases and future OS preview
builds require a field report; support must not be inferred from a neighboring
runner version.

The bounded `ORCHESTRATOR_NATIVE_ACCEPTANCE_REPORT` records:

- operating-system release, architecture and Python runtime;
- installed CLI version and hashes/sizes of diagnostic streams;
- normalized runtime capability fields;
- aggregate full-conformance soak counts and durations;
- explicit `not_tested` states for every field-only environment scenario.

It deliberately omits hostname, local paths and raw command output. The soak
uses synthetic fixtures and does not invoke an AI provider or require provider
credentials. A passing report proves the tested candidate's CLI/runtime
contracts at that point in time; it does not prove future provider quota or
external application behavior.

## Desktop field verification

Perform this check in the normal logged-in account with the exact Desktop and
CLI versions being reported:

1. Run the automated acceptance command and retain the report SHA-256.
2. Confirm the selected host CLI exposes the delivery command required by
   [Host setup](hosts.md). For Codex, check `codex queue --help` for `--thread`
   and `--message`.
3. From the live project chat, bind the host and run its bounded adapter
   diagnostic when one is available.
4. Start the host-scoped watcher and dispatch one harmless synthetic local
   check with terminal wake delivery enabled.
5. End the chat turn. Confirm that exactly one queued message becomes the next
   visible live turn, then inspect its durable event and delivery receipt.

Use this privacy-safe report body:

```text
Engine release/commit:
OS edition and version:
Architecture:
Python version:
Desktop application/version:
Host CLI/version:
Native acceptance status and report SHA-256:
Scenario: desktop_live_delivery | login_restart | sleep_resume | os_reboot |
  desktop_update | nonlocal_filesystem
Expected behavior:
Actual behavior:
Event ID and receipt status:
Security/cloud-filesystem constraints, if relevant:
Requested maintainer action:

Privacy confirmation:
- no hostname, account name or private project content;
- no credentials, raw provider output or unbounded logs;
- paths replaced with synthetic placeholders;
- only bounded failing-step evidence attached.
```

Durable queue acceptance without a visible live continuation is a delivery
finding, not proof that the worker or runtime failed.

## Field-only environment matrix

| Scenario | Hosted CI status | Required evidence |
| --- | --- | --- |
| Codex/Claude/VS Code live Desktop delivery | Not tested | Interactive session report |
| Login restart and OS reboot | Not tested | Service/status report after restart |
| Sleep/resume | Not tested | Pre/post operation identity and one terminal wakeup |
| Desktop or host CLI update | Not tested | Exact old/new versions and binding diagnostic |
| OneDrive, SMB/NFS or external volume | Not tested | Filesystem type plus atomic-write/locking result |
| Case-sensitive APFS or Windows/WSL shared path | Not tested | Exact topology and native acceptance result |
| Defender, Controlled Folder Access, TCC or Gatekeeper | Not tested | Sanitized policy state and failing operation class |

These are community/adopter verification categories, not implied product
guarantees. Do not convert an absent report into `supported` or `failed`.
Watchers are CLI-managed processes; OrchestratorEngine does not install an
operating-system startup service.
