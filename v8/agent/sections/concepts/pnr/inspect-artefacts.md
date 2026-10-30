## Inspect artefacts

| File in `artefacts/<run>/` | Contents |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output, written live, and the generated flow script |
| `<top>.def`, `<top>.routed.odb` | Routed design. `rb power` reads the ODB. |
| `<top>.routed.v`, `<top>.routed.sdc` | Logical netlist (no supply ports or supply pin connections) and constraints, for STA and gate-level simulation. The SDC is the primary corner's on a multi-corner platform. |
| `<top>.routed.pwr.v` | Powered netlist, for LVS against `<top>.gds`: the top's supply ports, last and sorted by name, and every instance's supply pins, fill and tap cells included, connected by the `pdn-config` global connections. It is the same whether the run was staged or one session. |
| `<top>.<corner>.routed.sdc` | Each corner's constraints, on a [multi-corner platform](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#sign-off-at-several-corners) |
| `<top>.routed.spef` | Extracted parasitics, when the PDK sets `rcx-rules` |
| `timing.rpt`, `electrical.rpt` | Worst paths and electrical violators, all corners |
| `route.drc.rpt`, `route.maze.log` | Detailed-route DRCs and router log |
| `grt_antennas.rpt`, `drt_antennas.rpt` | Antenna violations left after the antenna repair after global route and after detailed route; with `post-route-repair`, `drt_antennas.rpt` is the kept round's |
| `congestion.rpt` | Overflowing global-route tiles, only when global routing overflows. Loads in the OpenROAD GUI's DRC viewer. |
| `congestion-<i>.rpt` | The same at iteration `i`, every `global-route.congestion-report-iter-step` iterations of an overflowing route |
| `rtlmp/` | RTL-MP's reports, when it placed macros |
| `<top>.gds`, `<top>.png`, `klayout.*.log`, `def2stream.inputs.json` | Stream-out outputs, logs and the inputs it read |
| `checkpoints/`, `abstract/` | [Stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#keep-stage-checkpoints) and a [hardened block's](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#harden-a-block) views |

When OpenROAD fails, the run removes the routed database so `rb power` never reads a stale one; `pnr.log`, `pnr.tcl` and the congestion reports stay. A failed global route also leaves its database as a [checkpoint](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#keep-stage-checkpoints). A run that fails a later check (`routed_check`, `fail-on-electrical`, a signoff gate, `export`, `abstract`) keeps its routed outputs for inspection.
