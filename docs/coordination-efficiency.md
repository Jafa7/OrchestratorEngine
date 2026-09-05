# Coordination context measurement

This measurement answers one narrow question:

> While background work produces growing logs, how many UTF-8 bytes must a
> host chat read to check task state four times?

It does not measure total provider token use, model reasoning quality,
execution latency or total engineering productivity.

## Selective inspection contract

Context reduction does not mean that OrchestratorEngine truncates or deletes
worker output. The measured status path follows these rules:

- every task remains counted and its current state remains visible;
- the terminal state is visible on the final check;
- large-log sizes and paths are exposed without embedding log bodies;
- complete stdout, stderr, result and evidence artifacts remain on disk;
- an agent can open a targeted artifact or the complete log when needed.

The benchmark rejects a scenario if a task or current state disappears, a
terminal task is not reported as completed, or a full log/result/evidence
artifact is no longer available.

## Metric

The measurement uses UTF-8 response bytes because they are deterministic and
provider-neutral:

```text
context share = status report bytes / naive polling bytes
context reduction = 1 - context share
```

Actual token counts vary by model, tokenizer, tool protocol and host. UTF-8
bytes are a stable proxy for context volume, not a token-count promise.

## Baseline

Each scenario has four checkpoints at 25%, 50%, 75% and 100% of its final log
size. The deliberately simple baseline reads every cumulative worker stdout
log in full at every checkpoint.

The OrchestratorEngine path reads the complete pretty-printed JSON output of
`orchestrator-engine status` at the same four checkpoints. The status report
is not stripped down for the benchmark: it includes doctor, host capability,
worker profile, task and check summaries produced by the public command.
The harness pins the package-install health check to a stable synthetic
`benchmark` identity, replaces the temporary project root with
`/benchmark/project`, and fixes generated timestamps before counting bytes so
the result does not depend on checkout location or installation style.

This is a comparison with naive repeated log reads, not with every possible
manual tailing, IDE or log-analysis workflow.

## Fixture

The public deterministic fixture uses successful tasks and synthetic ASCII
logs. It invokes no AI model, provider API or private project data.

| Scenario | Tasks | Final log volume | Polls |
| --- | ---: | ---: | ---: |
| Long test | 1 | 256 KB | 4 |
| AI worker | 1 | 1 MB | 4 |
| Parallel workers | 3 | 3 x 512 KB | 4 |

Run it from a source checkout:

```bash
PYTHONPATH=src python3 benchmarks/coordination_context.py \
  --write-svg docs/assets/coordination-context.svg \
  --write-json docs/assets/coordination-context.json
```

The generated JSON is the machine-readable result behind the checked-in SVG.

## Results

![Context read while checking background work](assets/coordination-context.svg)

| Scenario | Naive polling | Status reads | Context share | Reduction |
| --- | ---: | ---: | ---: | ---: |
| Long test | 655,360 B | 16,572 B | 2.53% | 97.47% |
| AI worker | 2,621,440 B | 16,574 B | 0.63% | 99.37% |
| Parallel workers | 3,932,160 B | 19,161 B | 0.49% | 99.51% |

Every scenario passed the selective-inspection quality guard. The smaller
status reads come from carrying task state, diagnostics, sizes and artifact
paths instead of repeatedly embedding cumulative log bodies.

## Codex Desktop interpretation

Current Codex Desktop releases expose `codex queue`, so a detached watcher can
submit the completion to the live task without model polling. This benchmark
still measures a separate optimization: every explicit state check reads a
bounded status report instead of cumulative logs.

`worker wait` remains useful when the host task intentionally stays active or
when the installed Codex CLI lacks the queue command. It moves filesystem
checks outside the model, updates one terminal line and tells the user when the
worker finishes. Live wakeup reduces manual coordination but does not change
the measured byte ratios in this benchmark.

## Limitations

- Successful tasks normally need no log drill-down. A failure investigation
  that reads targeted excerpts or a full log consumes additional context.
- Larger status diagnostics or smaller worker logs reduce the percentage
  savings; larger or parallel logs increase it.
- The fixture does not include initial prompts, worker model output, final AI
  review, tool-call envelopes or host UI overhead.
- The three percentages are measured snapshots for these fixtures, not a
  universal forecast for every project or workflow.
