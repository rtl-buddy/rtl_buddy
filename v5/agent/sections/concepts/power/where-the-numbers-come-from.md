## Where the numbers come from

`rb power` runs on either the **post-synth** netlist (default) or the **post-PnR** routed design read back from the OpenROAD database (`<top>.routed.odb`). The `netlist-source:` field in `power.yaml` selects which.

| Aspect | `netlist-source: synth` (default) | `netlist-source: pnr` |
|---|---|---|
| Netlist | `synth_netlist.v` from `rb synth` (`read_verilog`) | `<top>.routed.odb` OpenROAD DB from `rb pnr` (`read_db`) |
| SDC | User-supplied `constraints:` | Post-CTS `<top>.routed.sdc` (or user `constraints:` if set) |
| Parasitics | None | Routing-derived wire-cap via `estimate_parasitics -global_routing` on the read-back ODB (no SPEF — OpenROAD's RCX extractor is not wired into the PnR flow, so `write_spef` would emit an empty file) |
| Clock tree | Flat (no CTS buffers) | Real CTS-buffered tree |
| Wire capacitance | None (zero) | Extracted from routing |
| Internal power | Gate-accurate (Liberty) | Gate-accurate (Liberty) |
| Leakage | Gate-accurate (Liberty) | Gate-accurate (Liberty) |
| Switching | **Under-estimated** | Realistic |
| Upstream run needed | `rb synth` | `rb synth` + `rb pnr` |

LEF is loaded for the `synth` source because OpenROAD's gate-level `read_verilog` requires a technology view in its in-memory DB; the `pnr` source restores it from the ODB instead. `report_power` itself only consults Liberty (per-cell internal/switching coefficients, leakage tables). If the routed ODB is missing the run FAILs with `routed ODB not found at … — re-run rb pnr (older runs predate the .routed.odb artefact)`, so re-run `rb pnr` if you upgraded across that change.

For early PPA exploration where you just want a leakage + activity-aware switching estimate, the synth-source path is fast and cheap. For sign-off-grade switching numbers where the clock tree matters, use the pnr-source path.
