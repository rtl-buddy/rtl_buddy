## Choose the design source

| Source | Input | Timing and parasitics | Required upstream runs |
| --- | --- | --- | --- |
| `netlist-source: synth` | `synth_netlist.v` | User SDC, no wire parasitics or clock tree | `rb synth` |
| `netlist-source: pnr` | `<top>.routed.odb` | Routed SDC, CTS, and the P&R run's extracted SPEF — or global-routing parasitic estimates when there is none | `rb synth`, then `rb pnr` |

The default `synth` source is useful for early leakage and activity comparisons but underestimates switching because it has no routed wire capacitance. Use `pnr` for a more representative post-route estimate.

If the routed ODB is missing, rerun `rb pnr`.

### Extracted parasitics

When the P&R run's PDK sets [`rcx-rules`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-the-process-dependent-steps), `rb pnr` writes an OpenRCX-extracted `<top>.routed.spef` beside the ODB, and a `netlist-source: pnr` power run reads it with `read_spef` after the routed SDC, in place of `estimate_parasitics -global_routing`. Without one — including the template's Nangate45 runs, which leave the key unset — the ODB handoff and the global-route estimate are used exactly as before.

A SPEF is read only when the P&R run that wrote the ODB vouches for it: its `pnr.tcl` must contain a `write_spef` command, and the SPEF must be no older than that `pnr.tcl`, which every `rb pnr` rewrites at the start of every run. A SPEF that fails either test is left unread with a `power.spef_rejected` WARNING naming the reason, and the run falls back to the estimate. That covers the case `rb pnr`'s own clearing cannot: an rtl_buddy that predates the SPEF reruns P&R and leaves the previous run's extraction beside a fresh ODB.

Which one was used is the run's `parasitics` result field — `spef` or `estimated`, absent on a `synth` source — shown in the summary's Parasitics column, logged as `power.parasitics`, and part of the model's options digest, so one ODB measured both ways is two experiments.

On the project template's flat sky130hd pipeclean (`demo_tiny_alu_subsys_sky130_flat_power`, static mode, default activity), switching power rose from 372 µW on the estimate to 467 µW on the extracted SPEF (+25 %; total 3.47 → 3.56 mW), with the clock network 269 → 315 µW and the registers 60 → 99 µW, while internal power (3.08 mW) and leakage (9.5 µW) — which do not depend on wire load — stayed put. The same P&R run's setup WNS went from +3.61 ns to +2.68 ns; nothing before extraction changed, and area and cell count are identical. The direction is the expected one: the global-route estimate prices wires as per-layer R and C along routing guides, while OpenRCX extracts the detailed routes themselves, vias and coupling capacitance included, and the estimate also skipped the segments it could not find a route for (`EST-0026 Missing route to pin` on seven reset pins) where extraction does not.
