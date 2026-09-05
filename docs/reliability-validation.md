# Reliability and Upgrade Validation

OrchestratorEngine validates repeated lifecycle behavior and historical state
compatibility without invoking an AI provider or reading private adopter data.
These checks are maintainer and CI tools; adopter projects do not need to run
them during ordinary work.

## Reliability soak

`tools/run_reliability_soak.py` repeatedly runs the installed
`conformance run` command. It stops on the first failure and emits one bounded
`ORCHESTRATOR_RELIABILITY_SOAK_REPORT` containing counts, durations and the
retained synthetic fixture pointer. It does not embed conformance steps, logs or
worker output.

```bash
python tools/run_reliability_soak.py \
  --cli /path/to/venv/bin/orchestrator-engine \
  --iterations 20 --mode full --timeout-seconds 15
```

The normal Linux CI wheel job runs 20 full iterations. Release-candidate or
investigation runs may raise the iteration count up to the bounded maximum of
1000. Repetition is not a substitute for deterministic regression tests: a
reproduced failure must be reduced to an owning-module test before it is
considered fixed.

## Historical upgrade matrix

`tools/verify_upgrade_path.py` creates a synthetic project with one installed
baseline CLI, then verifies it with a separate current CLI. The check proves:

- the current read-only `upgrade check` does not mutate baseline state;
- schema-compatible terminal event, result and evidence files remain present;
- the current watcher consumes the baseline signal exactly once;
- a second scan is idempotent.

```bash
python tools/verify_upgrade_path.py \
  --baseline-cli /path/to/baseline/bin/orchestrator-engine \
  --current-cli /path/to/current/bin/orchestrator-engine
```

CI covers published release wheels `0.10.0`, `0.11.1` and `0.12.0`. Their
download SHA-256 values are pinned in the workflow. The compatibility floor for
the planned `1.0` release is schema-version-1 state created by `0.10.0` or
newer. Earlier pre-release state remains inspectable through `doctor`, but is
not part of the automated upgrade guarantee.

Both tools use synthetic identifiers and temporary roots. Successful fixtures
are removed. Failed fixtures are retained when available so the maintainer can
inspect the exact local state transition without copying logs into a chat.
