## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing and zero DRC lines indicate a clean route. `wns_setup_ps`, `wns_hold_ps`, `tns_ps` (setup) and `tns_hold_ps` are converted to picoseconds from the Liberty `time_unit`; see [Synthesis: Configure tools and the PDK](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#configure-tools-and-the-pdk).

Three result fields count cells; compare experiments on `routed_cell_count`:

- `cell_count`, the summary's Cells column, is the input netlist's instance count at the floorplan. It leaves out every cell the flow adds.
- `routed_cell_count` is the finished design's instances, including tie, repair, clock-tree and hold buffers, excluding physical-only cells.
- `physical_cell_count` is the physical-only cells: masters of LEF class `CORE SPACER`, `CORE WELLTAP` or `ENDCAP*`, or matching a PDK `fill-cells` pattern. A decap counts only when its class is `CORE SPACER` or it is listed in `fill-cells`; sky130's `decap_*` cells are class `CORE`, so they count as routed unless listed.

The flow prints the last two after `>>> Final reports` as `RB-CELL-COUNT: routed <n> physical <m>`. If counting fails, both fields are absent and the run still passes.

A run passes when OpenROAD exits 0 with no `[ERROR ...]` line and, under `gds-mode: strict`, the requested export was delivered complete. It skips when `reglvl` filters it out or `tool:` is unsupported. Timing violations and DRC counts are metrics only; gate signoff on them in your project.

On a multi-corner platform, `wns_setup_ps`, `wns_hold_ps`, `tns_ps` and `tns_hold_ps` are the worst across corners. The result also names the `worst_setup_corner` and `worst_hold_corner` and lists each corner's own four values under `corners`, so setup at the slow corner and hold at the fast corner can be read separately; `pnr.log` has them after `>>> Per-corner timing`.
