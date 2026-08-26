## Where the numbers come from

`rb power` runs on either the **post-synth** netlist (default) or the **post-PnR** routed netlist with SPEF parasitics. The `netlist-source:` field in `power.yaml` selects which.

| Aspect | `netlist-source: synth` (default) | `netlist-source: pnr` |
|---|---|---|
| Netlist | `synth_netlist.v` from `rb synth` | `<top>.routed.v` from `rb pnr` |
| SDC | User-supplied `constraints:` | Post-CTS `<top>.routed.sdc` (or user `constraints:` if set) |
| Parasitics | None | `<top>.routed.spef` read via `read_spef` |
| Clock tree | Flat (no CTS buffers) | Real CTS-buffered tree |
| Wire capacitance | None (zero) | Extracted from routing |
| Internal power | Gate-accurate (Liberty) | Gate-accurate (Liberty) |
| Leakage | Gate-accurate (Liberty) | Gate-accurate (Liberty) |
| Switching | **Under-estimated** | Realistic |
| Upstream run needed | `rb synth` | `rb synth` + `rb pnr` |

LEF is loaded in both cases because OpenROAD's gate-level `read_verilog` requires a technology view in its in-memory DB; `report_power` itself only consults Liberty (per-cell internal/switching coefficients, leakage tables).

For early PPA exploration where you just want a leakage + activity-aware switching estimate, the synth-source path is fast and cheap. For sign-off-grade switching numbers where the clock tree matters, use the pnr-source path.
