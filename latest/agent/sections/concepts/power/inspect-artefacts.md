## Inspect artefacts

Outputs land under `<power-dir>/artefacts/<run>/`:

| File | Purpose |
| --- | --- |
| `power.tcl` | Generated OpenROAD script |
| `power.log` | OpenROAD output |
| `power.rpt` | Raw `report_power` report |

An FPGA run and a power run must not share a name within one suite: both own `artefacts/<name>/power.rpt` and the second to run overwrites the first. Ownership cannot be told apart by filename, so rtl_buddy does not try — give them distinct names.

On failure, inspect `power.log` for tool and input errors, then `power.rpt` for missing or malformed totals. `power.rpt` is deleted before each run and again if the run fails after writing it, so a run that never got as far as `report_power` — or that reached it and then failed — leaves none — read `power.log` in that case rather than an earlier run's numbers. A run that cannot find its backend tool is the exception: it deletes nothing, because a machine without the tool never ran it and has no business removing what a machine that has it produced.
