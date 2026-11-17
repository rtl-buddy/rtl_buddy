## Inspect artefacts

| File in `artefacts/<run>/` | Contents |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output, written live, and the generated flow script. |
| `<top>.def`, `<top>.routed.odb` | Routed design. `rb power` reads the ODB. |
| `<top>.routed.v`, `<top>.routed.sdc` | Netlist without supply ports or supply pin connections, and the primary corner's constraints, for STA and gate-level simulation. |
| `<top>.routed.pwr.v` | Powered netlist for LVS against `<top>.gds`: the top's supply ports, last and sorted by name, and every instance's supply pins (fill and tap cells too), connected by the `pdn-config` global connections. Staged and one-session runs write the same file. |
| `<top>.<corner>.routed.sdc` | Each corner's constraints on a [multi-corner platform](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners). |
| `<top>.routed.spef` | Extracted parasitics, with `rcx-rules`. |
| `timing.rpt`, `electrical.rpt` | Worst paths and electrical violators, all corners. |
| `worst_paths.rpt`, `fanout.rpt` | [Worst path per group and highest-fanout nets](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#find-the-critical-path-and-loaded-nets-in-the-rtl), with RTL names. |
| `route.drc.rpt`, `route.maze.log` | Detailed-route DRCs and router log. |
| `grt_antennas.rpt`, `drt_antennas.rpt` | Antenna violations left after the antenna repairs after global and detailed route; with `post-route-repair`, the kept round's. |
| `congestion.rpt`, `congestion-<i>.rpt` | Overflowing global-route tiles, only when global routing overflows; loads in the OpenROAD GUI's DRC viewer. The `-<i>` files are iteration `i`, every `global-route.congestion-report-iter-step` iterations. |
| `rtlmp/` | RTL-MP's reports, when it placed macros. |
| `<top>.gds`, `<top>.png`, `klayout.*.log`, `def2stream.inputs.json` | Stream-out outputs, logs and inputs read. |
| `checkpoints/`, `abstract/` | [Stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) and a [hardened block's](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block) views. |

- When OpenROAD fails, the run removes the routed database so `rb power` never reads a stale one. `pnr.log`, `pnr.tcl` and the congestion reports stay, and a failed global route leaves its database as a [checkpoint](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints).
- A run that fails a later check (`routed_check`, `fail-on-electrical`, a signoff gate, `export`, `abstract`) keeps its routed outputs.
