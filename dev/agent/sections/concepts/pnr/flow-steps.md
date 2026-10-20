## Flow steps

The generated `pnr.tcl` runs these steps in order:

1. Source `platform-tcl` when set, read Liberty, LEF, the synthesis netlist and the SDC, set the design's max fanout when `max-fanout` is set, then source `layer-rc-tcl` when set.
2. Initialize the floorplan, create routing tracks and run `insert_tiecells` for the PDK's `tie-hi` and `tie-lo` ports, one tie cell per constant net.
3. Place macros, insert tap and endcap cells when `tapcell-tcl` is set, build the power grid and place the IO pins, then run `buffer_ports -inputs -outputs` when the run buffers its ports (`buffer-ports`, on by default for `harden: true`; see [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block)).
4. Run global placement, with `-reference_hpwl` when `placement.reference-hpwl` is set and `-routability_driven` / `-routability_use_grt` when `placement.routability-driven` / `placement.routability-use-grt` are `true`, then `repair_tie_fanout` for each tie port, which gives every constant-driven load its own tie cell `placement.tie-separation` microns away (default 0).
5. Run `repair_design`, legalization, clock-tree synthesis (with `-apply_ndr` when `cts-apply-ndr` is set), setup repair when `post-cts-setup-repair` is set, hold repair and a final legalization.
6. Route globally with `-verbose` and a congestion report, after `set_global_routing_layer_adjustment` when `routing-layer-adjustment` is set, repair hold on the global-route parasitics when `global-route-hold-repair` is set, route in detail at the run's `detailed-route-verbose` level, insert fill, extract parasitics when `rcx-rules` is set, then write reports and outputs.

A tie port the PDK leaves unset gets neither tie step.
