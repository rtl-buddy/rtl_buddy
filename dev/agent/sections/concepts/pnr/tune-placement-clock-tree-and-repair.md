## Tune placement, clock tree and repair

Process-dependent settings live on the PDK and can be overridden on the platform, most also on a run, so variants of one block need no platform each. The [YAML reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#synthesis-and-physical-design-tools) gives each key's levels and default. A default is OpenROAD's or ORFS's, except for rb steps without a counterpart (such as `placement.macro-pin-limits`) and [known issue](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/) workarounds.

| Problem | Setting |
| --- | --- |
| Global route overflows | Lower `placement.density` (default 0.7) or `floorplan.utilization`. Try `placement.routability-driven: true`, plus `routability-use-grt: true` for a slower, closer estimate. A fixed `placement.reference-hpwl`, such as `446000000`, spreads a large design further. |
| DRCs left in congested areas | `routing-layer-adjustment` (0 to 1) withholds that fraction of each signal layer from the global router; [`global-route.layer-adjustment`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-global-routing) sets it per layer. |
| Setup misses on long nets | `placement.timing-driven: true`, which needs wire RC. On OpenROAD 26Q4 it also buffers and resizes during placement, growing area. |
| Timing misses after routing | [`global-route-repair` or `post-route-repair`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-timing-after-routing). |
| Long nets that routing pushes over their slew limit | `placement.repair-design: {max-wire-length: <µm>}`, below. |
| Many fanout buffers on a library without a fanout limit (sky130hd) | `max-fanout: 100000` stops buffering for fanout alone; `repair_design` otherwise uses 50. |
| Cells too tight | `placement.padding` (sites). |
| Clock tree | `cts-buffer`, `cts-sink-clustering`, `cts-obstruction-aware`, `cts-apply-ndr`. `cts-clock-nets` builds trees for the named clocks only. |
| A strong root buffer with small branch buffers | `cts-root-buffer` names the root, and `cts-buffer` lists the branch buffers only. Otherwise CTS picks the root from `cts-buffer`. |
| Slew violations on clock pins far from the nearest leaf buffer, as when many clock gates sit in a small area | `cts-sink-clustering` with `size` and `max-diameter` (µm). |
| Setup misses on flops of a clock branch CTS padded to match the deepest (`CTS-0033`, `CTS-0036`), as when a clock drives a buffer as well as flops | `cts-latency-balancing: false`. |
| Long clock wires after CTS | `cts-repair-clock-nets: true`, or `{max-wire-length: <µm>}`. |
| A block must leave the top layers to its parent | `routing-layers` on the run. |
| A block needs other tap spacing or endcaps | `tapcell-tcl` on the platform or run. |
| Cells crowd macro edges, or Metal Spacing DRCs at a block edge | `placement.macro-cell-halo` (µm), or a `tapcell-tcl` that cuts rows around macros. |
| Tie cells too close to their loads | `placement.tie-separation` (µm). |
| Macro pins over a tight Liberty limit, such as OpenRAM SKY130 SRAM inputs (0.04 ns) | `placement.macro-pin-limits`, below. |

A platform that ends up with `routability-use-grt` but not `routability-driven` fails to load.

Each CTS setting sits on the platform and the run, and a run's value replaces the platform's whole:

```yaml
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4]
    cts-root-buffer: sky130_fd_sc_hd__clkbuf_16
    cts-sink-clustering: {size: 20, max-diameter: 25}
    cts-obstruction-aware: false
    cts-repair-clock-nets: {max-wire-length: 1000}
```

This calls `clock_tree_synthesis -root_buf sky130_fd_sc_hd__clkbuf_16 -buf_list sky130_fd_sc_hd__clkbuf_4 -sink_clustering_enable -sink_clustering_size 20 -sink_clustering_max_diameter 25 -no_obstruction_aware`, then `repair_clock_nets -max_wire_length 1000`.

### Threshold-voltage flavours and footprints

On a library with several threshold-voltage flavours, such as ASAP7 RVT and LVT with both flavours' Liberty per corner and both LEFs in `macro-lef`, resizing moves cells between flavours freely.

- `keep-sizing-vt: true` keeps a cell's flavour when `repair_design` or `repair_timing` resizes it; only timing repair's threshold-voltage swap changes it.
- `dont-use-cells` keeps a flavour out of every step.
- `cts-dont-use-cells` keeps cells out of the clock tree only: CTS and `repair_clock_nets` skip them, and post-CTS repair may still use them on data paths.
- It governs only cells CTS picks itself, dummy-load inverters and inferred delay buffers. Neither CTS nor `repair_clock_nets` checks `cts-buffer` or `cts-root-buffer` against either list, so name the flavour there.

`match-cell-footprint: true` on the PDK or platform makes resizing keep each cell's footprint, as ORFS does for platforms that set `MATCH_CELL_FOOTPRINT` (sky130hd and ihp-sg13g2 among them). It adds `-match_cell_footprint` to every `repair_design` and `repair_timing` the flow emits, the post-route and electrical rounds included, but not those a hook runs; pass the flag there yourself. rb runs no `repair_timing -recover_power`, which ORFS also gives the flag.

### Bound wire length during placement

`placement.repair-design` passes `slew-margin`, `cap-margin` and `max-wire-length` to the place stage's `repair_design`, as `global-route-repair.repair-design` does after global route. Unset, it uses OpenROAD's defaults, as ORFS does.

```yaml
    placement:
      repair-design: {max-wire-length: 600, slew-margin: 10}
```

`repair_design` judges wires on the placement estimate, which a long routed net can exceed. `max-wire-length` splits any longer wire with a buffer, so one driver does not carry a long chain to routing, where post-route repair may not see it ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#post-route-repair_design-sizes-its-buffering-on-the-placement-estimate)).

### Buffer macro pins with tight limits

`repair_design` sizes repeaters for the library's target slew, so a macro pin with a much lower limit stays in `electrical.rpt`. `placement.macro-pin-limits` buffers such pins at the end of the place stage, after `repair_design`, the `after-repair-design` hook and legalization:

```yaml
    placement:
      macro-pin-limits:
        buffers: [sky130_fd_sc_hd__buf_1, sky130_fd_sc_hd__buf_2, sky130_fd_sc_hd__buf_4,
                  sky130_fd_sc_hd__buf_8, sky130_fd_sc_hd__buf_12, sky130_fd_sc_hd__buf_16]
        margin: 0.05              # of each max-slew limit, kept in hand
        capacitance-margin: 0.5   # of each max-capacitance limit; default: margin
```

- Each macro data pin over its limit less the margin gets a buffer fixed on the nearest free row site; an input also gets a pre-driver. Buffers are sized from `buffers` weakest first and marked `dont_touch`, so later repair leaves them alone.
- When no pair meets the limit less the margin, the closest pair stays (for an input, the lowest slew), which is not always the strongest, and the pin counts in `macro_pin_unmet_count`.
- `pnr.log` has one `>>>` line per pin.

The step judges on placement parasitics, so check `electrical.rpt`:

- A long macro-output net can route to twice its estimated capacitance, hence `capacitance-margin: 0.5` on sky130hd.
- A large `macro-cell-halo` keeps the buffer too far from the pin: on sky130hd, 3 µm works with `buf_8` and 20 µm does not.
- A pin route that global routing detours around the macro is out of reach.

### Optimisation steps

The flow runs ORFS's default optimisation steps. Each key below turns one off, on the platform or the run, at the cost in the last column:

| Step | Where | Key | Gives up |
| --- | --- | --- | --- |
| `repair_timing -setup`, buffer removal and sizing only | End of floorplan | `floorplan-setup-repair` | Setup slack |
| `improve_placement`, then `optimize_mirroring` | After place legalization | `placement.improve-placement`, `placement.optimize-mirroring` | Shorter wires |
| `repair_clock_inverters` | Before CTS | `cts-repair-clock-inverters` | Keeping CTS from buffering inverted clocks |
| `repair_design`, setup and hold `repair_timing` | After global route | `global-route-repair: null` ([details](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-timing-after-routing)) | Setup slack |
| `repair_antennas` (5 iterations), `check_antennas` into `grt_antennas.rpt` | After global-route repair and its hooks | `global-route-antenna-repair` | Antenna repair, which no other step does |
| `repair_antennas` and detailed route while violations remain (at most 5), `check_antennas` into `drt_antennas.rpt` | After detailed route and each post-route round's | `detailed-route-antenna-repair` | Antenna repair |

Antenna repair inserts the PDK's antenna diodes, LEF class `CORE ANTENNACELL`. A [probe](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#probe-a-floorplan) skips the floorplan setup repair, the global-route repair and the antenna repair after global route.
