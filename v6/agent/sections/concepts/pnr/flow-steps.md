## Flow steps

The generated `pnr.tcl` runs these steps in order:

1. Source `platform-tcl` when set, read Liberty, LEF, the synthesis netlist and the SDC, then source `layer-rc-tcl` when set.
2. Initialize the floorplan, create routing tracks and run `insert_tiecells` for the PDK's `tie-hi` and `tie-lo` ports, one tie cell per constant net.
3. Place macros, insert tap and endcap cells when `tapcell-tcl` is set, build the power grid and place the IO pins.
4. Run global placement, then `repair_tie_fanout` for each tie port, which gives every constant-driven load its own tie cell `placement.tie-separation` microns away (default 0).
5. Run `repair_design`, legalization, clock-tree synthesis, setup repair when `post-cts-setup-repair` is set, hold repair and a final legalization.
6. Route globally, after `set_global_routing_layer_adjustment` when `routing-layer-adjustment` is set, and in detail, insert fill, extract parasitics when `rcx-rules` is set, then write reports and outputs.

A tie port the PDK leaves unset gets neither tie step.
