## What the marker covers

A marker excuses only a verdict the flow's own tool reported at its own end: a simulation that ran and printed `FAIL`, a property sby disproved, a violation count, a gate the tool evaluated. A failure that happened *instead of* a verdict is graded `FAIL` however the marker is spelled, because the run never reached the behavior the marker is about:

| Flow | Graded `FAIL` under a marker |
| --- | --- |
| `test` | preproc, sweep, and filelist setup failures; compile failures; a sim killed at `sim_timeout`; a simulator that exited without a verdict; a dispatched job the scheduler lost |
| `fpv` | sby reporting `UNKNOWN`, a solver timeout, or an error instead of `PASS`/`FAIL` |
| `cdc`, `lint` | the analyzer failing before it produced a count |
| `synth`, `pnr`, `power`, `fpga` | setup failures — a missing tool, an unresolvable platform or PDK, a filelist or script-generation error |

A run graded this way says why, so the reason is in the summary table and in `result.json`:

```
<suite>  <test>  FAIL  xfail not applied (sim timeout): Sim hit timeout
```

The result carries a `fail_stage` key naming the stage (`setup`, `compile`, `sim_timeout`, `sim`, `dispatch`, `tool`), and the `*_suite.xfail` event carries `excused: false` with the same reason for machine consumers. A negative control that stopped compiling, or that was killed on a busy node before reaching the mismatch it exists to catch, therefore fails the run rather than reporting green.

A tool-reported failure in `synth`, `pnr`, `power`, and `fpga` is that flow's verdict — those flows have no later verdict stage — so a marker still covers a synthesis the tool rejected.

See [YAML Formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/) for the field on each configuration type.
