## Inspect artefacts

Outputs land under `<power-dir>/artefacts/<run>/`:

| File | Purpose |
| --- | --- |
| `power.tcl`, `power.log` | Generated OpenROAD script and its output |
| `power.rpt` | Raw `report_power` report (worst corner on a multi-corner platform) |
| `power.<corner>.rpt` | Each corner's report, multi-corner platforms only |
| `power_netlist.v` | This run's copy of the upstream netlist, the file OpenROAD reads |
| `power_instances.rpt` | Raw `report_power -instances`, one line per leaf instance |
| `phys-model.json`, `phys-manifest.json` | Physical model and its manifest; query with `rb phys` ([Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/phys/)) |

With `phys-run` set, the model and manifest are written to the synthesis run's directory instead.

An FPGA run and a power run must not share a name within one suite. Both own `artefacts/<name>/power.rpt`, and the second to run overwrites the first.
