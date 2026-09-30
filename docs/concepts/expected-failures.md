---
description: Mark known failures, choose whether an unexpected pass should fail a run, and know which failures a marker never covers.
---

# Expected Failures

An expected-failure marker keeps a known, understood failure visible in a suite without failing the run. The fields are available on runs in `tests.yaml`, `fpv.yaml`, `synth.yaml`, `pnr.yaml`, `power.yaml`, `fpga.yaml`, `cdc.yaml` and `lint.yaml`; see [YAML Formats](../reference/yaml.md) for each.

## Choose strictness

| Marker | Actual failure | Unexpected pass | Use when |
| --- | --- | --- | --- |
| `xfail: true` | `XFAIL`, counts as pass | `XPASS`, counts as pass | Either outcome is acceptable |
| `xfail_strict: true` | `XFAIL`, counts as pass | `XPASS`, counts as fail | A pass means the marker is stale |

- If both are set, strict wins.
- `SKIP` and `NA` are unchanged. A marker does not cover an unknown `NA`, which still exits 1.
- Prefer `xfail_strict: true` for a known bug or an intentionally failing teaching case, so the regression reports when the behavior changes.

## What the marker covers

A marker excuses only a verdict the flow's own tool reported at its end: a simulation that ran and printed `FAIL`, a property sby disproved, a violation count, a gate the tool evaluated. A failure that happened instead of a verdict is graded `FAIL` whatever the marker says, because the run never reached the behavior the marker is about.

| Flow | Graded `FAIL` under a marker |
| --- | --- |
| `test` | preproc, sweep and filelist setup failures; compile failures; a sim killed at `sim_timeout`; a simulator that exited without a verdict; a dispatched job the scheduler lost |
| `fpv` | sby reporting `UNKNOWN`, a solver timeout, or an error instead of `PASS`/`FAIL` |
| `cdc`, `lint` | the analyzer failing before it produced a count |
| `synth`, `pnr`, `power`, `fpga` | setup failures: a missing tool, an unresolvable platform or PDK, a filelist or script-generation error |

In `synth`, `pnr`, `power` and `fpga`, a failure the tool itself reports is that flow's verdict, so a marker still covers a synthesis the tool rejected.

## Read a marker that was not applied

The summary table and `result.json` give the reason:

```text
<suite>  <test>  FAIL  xfail not applied (sim timeout): Sim hit timeout
```

- The result carries `fail_stage`: `setup`, `compile`, `sim_timeout`, `sim`, `dispatch` or `tool`.
- The `*_suite.xfail` event carries `excused: false` with the same reason, for machine consumers.
- A negative control that stopped compiling, or was killed before reaching the mismatch it exists to catch, therefore fails the run instead of reporting green.
