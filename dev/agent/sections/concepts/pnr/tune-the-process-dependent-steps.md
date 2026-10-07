## Tune the process-dependent steps

Placement, clock-tree and power-grid steps default to values calibrated for Nangate45. Another process declares its own. Every key below is optional.

```yaml
cfg-pdks:
  - name: sky130hd
    # ...
    placement:
      density: 0.55        # default 0.7
      padding: 2           # default 1, in sites
      macro-halo: 30.0     # default 20.0, in microns
    dont-use-cells: ["sky130_fd_sc_hd__probe*"]
    pdn-config: pdk/sky130hd/pdn.tcl
    rcx-rules: pdk/sky130hd/rcx_patterns.rules

cfg-pnr-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4, sky130_fd_sc_hd__clkbuf_8]
    placement:
      density: 0.6         # this platform only; padding stays the PDK's 2
```

- **`placement.density`** is the global-placement target utilization, above 0 and at most 1. **`placement.padding`** is cell padding in sites. A platform overrides either one field by field.
- **`placement.macro-halo`** (default 20.0 µm) is the channel kept between macros and between a macro and each core edge. Below about 19 µm on sky130hd, `pdngen` fails with `PDN-0179`. Raise it for a coarser grid; lower it only when macros do not fit.
- **`placement.macro-cell-halo`** (default 1.0 µm) keeps standard cells off macros with a hard blockage. Without it, the router can report a Metal Spacing violation at a pin on a hardened block's edge. `0` disables it.
- **`placement.tie-separation`** (default 0 µm) is how far from its load each per-load tie cell is placed; see [Flow steps](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#flow-steps).
- **`placement.reference-hpwl`** (positive number, unset by default) passes `-reference_hpwl` to `global_placement`. Unset, the placer's density-penalty controller derives its reference HPWL from the design size, which on a large design spreads cells less and ends routability inflation early, so global route can overflow. A fixed value such as `446000000` spreads cells further and can clear the overflow.
- **`placement.routability-driven`** (default `false`) adds `-routability_driven` to `global_placement`: the placer estimates congestion, inflates cells in congested regions and re-spreads them. Turn it on for a congested block whose global route overflows. **`placement.routability-use-grt`** (default `false`) adds `-routability_use_grt`, which estimates that congestion with the global router instead of RUDY; slower, closer to the real route. It takes effect only in routability mode, so a platform whose resolved settings enable it without `routability-driven` fails to load. Like the other `placement.*` keys, each is set on the PDK and overridden per platform.
- **`cts-buffer`** takes a name or a list. With a list, the first entry is the root buffer.
- **`cts-sink-clustering`** (default `true`) passes `-sink_clustering_enable` to CTS. Set it to `false` when CTS fails with `CTS-0080 Sink not found`, as it can on coincident clock pins or a large ASAP7 clock tree.
- **`cts-apply-ndr`** (unset by default) passes `-apply_ndr` (`none`, `root_only`, `half` or `full`) to CTS; unset keeps OpenROAD's default, `half`. Set `none` when the global router detours clock nets that carry non-default rules to several times their Manhattan length, which shows as large hold violations and `GRT-0273 Disabled NDR` in the log.
- **`max-fanout`** (positive integer, unset by default) adds `set_max_fanout <n> [current_design]` after each `read_sdc` in `rb pnr` and `rb power`, replacing a design-level limit from the SDC. When neither the Liberty nor the SDC sets a limit, `repair_design` uses 50 and buffers every larger net; on a library without one, such as SKY130 HD, that costs setup timing. A large value such as `100000` stops repair buffering for fanout alone.
- **`post-cts-setup-repair`** (default `false`) runs `repair_timing -setup` after CTS, before hold repair. Turn it on when the post-CTS netlist misses setup; it adds buffers and resizes cells, so QoR changes.
- **`global-route-hold-repair`** (default `false`) repairs hold again after global route, on `estimate_parasitics -global_routing`, then legalizes and reroutes the new buffers before detail route, as ORFS does; see [Set wire RC](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#set-wire-rc).
- **`routing-layer-adjustment`** (0 to 1, unset by default) withholds that fraction of each signal layer's capacity from the global router (`set_global_routing_layer_adjustment`, ORFS `ROUTING_LAYER_ADJUSTMENT`, 0.25 on ASAP7). Raise it when detailed routing ends with DRCs in congested areas; unset leaves the router's default.
- **`pdn-config`** is a path, resolved from `root_config.yaml`, to a Tcl snippet that declares the power grid (`add_global_connection`, `set_voltage_domain`, `define_pdn_grid`, `add_pdn_stripe`, `add_pdn_connect`). The flow sources it after macro placement and calls `pdngen` itself, so the snippet must not. Unset means no power grid.
- **`dont-use-cells`** and **`rcx-rules`** are described below, and the platform Tcl hooks under [Source platform Tcl hooks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#source-platform-tcl-hooks).
