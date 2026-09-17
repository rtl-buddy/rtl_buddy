## Inspect artefacts

Outputs land under `<power-dir>/artefacts/<run>/`:

| File | Purpose |
| --- | --- |
| `power.tcl` | Generated OpenROAD script |
| `power.log` | OpenROAD output |
| `power.rpt` | Raw `report_power` report |
| `power_netlist.v` | This run's copy of the upstream netlist, the file OpenROAD reads |
| `power_instances.rpt` | Raw `report_power -instances` report, one line per leaf instance |
| `power_instances.cells` | Instance path to Liberty cell, the module column that report lacks |
| `phys-model.json` | Physical model — per-instance rows plus the design totals |
| `phys-manifest.json` | Which physical artefacts this run produced, and where |
| `phys-publish.lock` | Mutex held while that pair is rewritten; empty between publishes |

Query the model with `rb phys`; see [Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/phys/).

`phys-model.json` and `phys-manifest.json` are the exception to the table above: with `phys-run` set they are written into the synthesis run's artefact directory instead, and everything else stays here. See [Pair the model with a synthesis run](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#pair-the-model-with-a-synthesis-run).

The per-instance half is a by-product, never a gate: the design totals are parsed and reported before it is read, and the hierarchy walk that produces it runs inside a Tcl `catch`. An OpenSTA that cannot produce it costs the model its `instances` block — `null`, with a warning — and the run still reports `PASS`. A `rb synth` run publishing into the same artefact directory for the same top fills the model's per-module half rather than replacing it. Either half travels only when both runs recorded the same netlist hash: this run keeps the per-module rows already there when the netlist it read is the one they were counted off, and a *later* synthesis keeps these per-instance rows when the netlist it writes hashes equal to the one this run read. The two flows are still not symmetric, but the check is — the ordinary `rb synth` then `rb power` pair passes it because a power run reads exactly what the synthesis wrote, while a rebuild between the two runs fails it in whichever direction publishes second. A `netlist-source: pnr` run reads the routed database rather than a netlist and records no such hash, so it inherits no per-module rows and its own rows are never carried forward by a synthesis. The netlist a `netlist-source: synth` run measures is copied into its own artefact directory as `power_netlist.v` first, and it is that copy OpenROAD reads and that copy the hash identifies. The bytes measured and the bytes named are therefore the same file, which no other command writes: a synthesis landing in the upstream directory mid-analysis is caught rather than recorded as a match. The manifest names that copy alongside the hash, so a result read back from an archive reaches the netlist the numbers were measured over and can re-check the hash against it; a `netlist-source: pnr` run names none. The copy is removed before each run and again if the run fails, like the reports beside it.

An FPGA run and a power run must not share a name within one suite: both own `artefacts/<name>/power.rpt` and the second to run overwrites the first. Ownership cannot be told apart by filename, so rtl_buddy does not try — give them distinct names.

On failure, inspect `power.log` for tool and input errors, then `power.rpt` for missing or malformed totals. `power.rpt` is deleted before each run and again if the run fails after writing it, so a run that never got as far as `report_power` — or that reached it and then failed — leaves none — read `power.log` in that case rather than an earlier run's numbers. A run that cannot find its backend tool is the exception: it deletes nothing, because a machine without the tool never ran it and has no business removing what a machine that has it produced.
