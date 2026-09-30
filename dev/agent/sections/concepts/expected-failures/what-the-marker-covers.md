## What the marker covers

A marker excuses only a verdict the flow's own tool reported at its end: a simulation that ran and printed `FAIL`, a property sby disproved, a violation count, a gate the tool evaluated. A failure that happened instead of a verdict is graded `FAIL` whatever the marker says, because the run never reached the behavior the marker is about.

| Flow | Graded `FAIL` under a marker |
| --- | --- |
| `test` | preproc, sweep and filelist setup failures; compile failures; a sim killed at `sim_timeout`; a simulator that exited without a verdict; a dispatched job the scheduler lost |
| `fpv` | sby reporting `UNKNOWN`, a solver timeout, or an error instead of `PASS`/`FAIL` |
| `cdc`, `lint` | the analyzer failing before it produced a count |
| `synth`, `pnr`, `power`, `fpga` | setup failures: a missing tool, an unresolvable platform or PDK, a filelist or script-generation error |

In `synth`, `pnr`, `power` and `fpga`, a failure the tool itself reports is that flow's verdict, so a marker still covers a synthesis the tool rejected.
