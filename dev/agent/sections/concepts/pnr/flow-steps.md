## Flow steps

The generated `pnr.tcl` runs these steps in order:

1. Read Liberty, LEF, the synthesis netlist and the SDC.
2. Initialize the floorplan and run `insert_tiecells` for the PDK's `tie-hi` and `tie-lo` ports, one tie cell per constant net.
3. Place macros, build the power grid and place the IO pins.
4. Run global placement, then `repair_tie_fanout` for each tie port, which gives every constant-driven load its own tie cell `placement.tie-separation` microns away (default 0).
5. Run `repair_design`, legalization, clock-tree synthesis, hold repair and a final legalization.
6. Route globally and in detail, insert fill, extract parasitics when `rcx-rules` is set, then write reports and outputs.

A tie port the PDK leaves unset gets neither tie step.
