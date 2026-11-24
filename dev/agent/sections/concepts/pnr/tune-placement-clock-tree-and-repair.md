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
| The clock must reach chosen registers, such as a block's boundary registers, at a set insertion delay | `clock-latency` reports their routed arrival against a target, [below](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#report-clock-arrival-at-chosen-sinks). |
| A buffer that forwards the clock to an output port must stay on the port's net, not become a leaf of the register tree | `cts-root-taps`, [below](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#forward-a-clock-with-a-root-tap). |
| Setup misses on flops of a clock branch CTS padded to match the deepest (`CTS-0033`, `CTS-0036`), as when a clock drives a buffer as well as flops | `cts-latency-balancing: false`. |
| Long clock wires after CTS | `cts-repair-clock-nets: true`, or `{max-wire-length: <µm>}`. |
| A block must leave the top layers to its parent | `routing-layers` on the run. |
| A block needs other tap spacing or endcaps | `tapcell-tcl` on the platform or run. |
| Cells crowd macro edges, or Metal Spacing DRCs at a block edge | `placement.macro-cell-halo` (µm), or a `tapcell-tcl` that cuts rows around macros. |
| A cell that every repair must leave as it is, such as a clock-forwarding buffer | `dont-touch`, [below](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-instances-unchanged). |
| Tie cells too close to their loads | `placement.tie-separation` (µm). |
| Macro pins over a tight Liberty limit, such as OpenRAM SKY130 SRAM inputs (0.04 ns) | `placement.macro-pin-limits`, below. |
| Legalization after routing repair is slow, or fails with `DPL-0701` then `DPL-0033` | `placement.legalizer`, below. |

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

### Forward a clock with a root tap

TritonCTS puts a buffer it inserts in front of every input pin on a clock net. A block that forwards its clock to an output port through its own buffer wants that buffer's input on the port's net, so the forwarded clock's delay is the one buffer and its wire. `cts-root-taps` names such input pins, each `<instance>/<pin>`, brackets literal in both parts (`u_ram/CLK[0]`):

```yaml
    dont-touch: [u_fwd.u_clk_buf]
    cts-root-taps: [u_fwd.u_clk_buf/A]
```

- Just before `clock_tree_synthesis`, after the `before-cts` hooks and `repair_clock_inverters`, each pin is taken off its net, and the log names it. After CTS and `repair_clock_nets`, and before the `after-cts` hooks, it goes back on the net of the same name: the root net, beside the tree's root buffer. The result's `cts_root_taps` lists each pin with that net. The `before-repair-clock-nets` hooks run while the pins are off.
- A dont-touch instance, from [`dont-touch`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-instances-unchanged) or a hook's `set_dont_touch`, cannot have a pin taken off (`ODB-0370`), so the mark is cleared for the two steps and set again.
- In a flat run the forwarded clock can clock registers further on, directly or through gates. While the pin is off, that clock would stop at the buffer, so the flow defines a temporary clock on each of the buffer's outputs that carries it to a register or macro clock pin, as the timer's clock network runs, with the tap's clock's period and waveform, and CTS builds that tree too. An output that an SDC clock already starts on, such as a `create_generated_clock` on the buffer's output, keeps that clock instead. A temporary clock is named `rb_cts_root_tap_<n>`, skipping names the SDC uses.
- The flow fixes, during CTS, each tapped instance that drives clock pins, which CTS would otherwise clone as a clock gate with a copy of the input that is off, and each gate on a temporary clock's net, which CTS would clone where it would not on the original net. All is read from the timer before any pin is taken off, and undone after CTS.
- With `cts-clock-nets`, CTS builds trees for the named nets only: name the net the buffer drives too, or its registers get no tree.
- A pattern that matches no pin, a pin that is not an input on a clock net, or a net gone after CTS fails the CTS stage, naming the entry. On a top without rows, where CTS does not run, the pins stay where they are.

### Report clock arrival at chosen sinks

Abutted blocks that forward a clock to each other agree on the clock's arrival at their boundary registers. CTS balances a tree but takes no target, so `clock-latency` reports the arrival at named groups of sinks, to check against one:

```yaml
    clock-latency:
      - {name: boundary, sinks: [input-capture, output-launch], target-ps: 90, tolerance-ps: 5}
      - {name: fwd_regs, sinks: "u_core.r_*/CLK"}
```

- `sinks` takes `input-capture` (the registers whose data pin captures an input port through combinational logic; an asynchronous set or reset, or a clock gate's enable, does not count), `output-launch` (those that launch an output port), and `<instance>/<pin>` names or globs, as `cts-root-taps` takes them. A pin off the clock network, such as a data pin, counts in `sinks` but not `timed_sinks`.
- The final reports print each group's min, max and mean late arrival of the clock's rising edge at each corner, on the final parasitics (`RB-CLOCK-LATENCY:` lines). For a clock whose rising edge is at 0, that is its source latency plus the insertion delay. With [a mode per corner](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners), each corner is measured in its own SDC mode, so a sink that only one corner's case analysis clocks is timed at that corner.
- The result's `clock_latency.<group>` has `min_ps`, `max_ps` and `mean_ps` at the platform's primary corner, `corners` with each corner's, `sinks`, `timed_sinks`, `target_ps` and `tolerance_ps`. With a tolerance, `within_tolerance` says whether every sink is within it of the target at the primary corner, and a group outside it warns `pnr.clock_latency_off_target`. A group with no sink, or a sink no clock reaches, warns `pnr.clock_latency_unclocked`.
- Only a run that reaches its final reports measures the groups; a probe or `--stop-after` run does not. Nothing fails on a group off target.

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
- A pin of a [`dont-touch`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-instances-unchanged) macro is left unbuffered, since OpenROAD cannot move it to a new net, and counts in `macro_pin_unmet_count`.
- `pnr.log` has one `>>>` line per pin.

The step judges on placement parasitics, so check `electrical.rpt`:

- A long macro-output net can route to twice its estimated capacitance, hence `capacitance-margin: 0.5` on sky130hd.
- A large `macro-cell-halo` keeps the buffer too far from the pin: on sky130hd, 3 µm works with `buf_8` and 20 µm does not.
- A pin route that global routing detours around the macro is out of reach.

### Choose the legalizer per stage

OpenROAD 26Q4 legalizes with one of two engines:

- Negotiation, the default. Cells compete for sites over many iterations. It adds less wire length where a step crowds many cells in, such as hold repair after CTS.
- Diamond search, `-use_diamond_legalizer`. Each cell takes a nearby free site. It is much faster, and as good where a step adds few cells, such as the repairs after global and detailed route. On a crowded design negotiation can stop at its iteration cap (`DPL-0701`) with cells still overlapping, which fails `check_placement` (`DPL-0033`).

`placement.legalizer` picks the engine per stage. This keeps negotiation through CTS and uses diamond search from global route on:

```yaml
    placement:
      legalizer: {global_route: diamond, post_route_repair: diamond}
```

- `legalizer: diamond` sets every stage. A stage the mapping leaves out passes no option, so OpenROAD's default applies.
- It applies to every `detailed_placement` rb runs in the stage, `placement.macro-pin-limits` included. A hook passes its own options.
- With `rotated-macro-footprint: diamond-legalizer`, a turned macro makes every stage use diamond search, including one set to `negotiation`.
- Diamond search stays on for the rest of an OpenROAD session ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#diamond-search-stays-on-for-the-rest-of-an-openroad-session)). A one-session run that sets a stage to `negotiation` after one set to `diamond` fails at setup; run it with [`stages: true`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions). Each post-route round is a session of its own.
- Each stage's engine is in that stage's script only. A [resume](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions) that changes it for stages from `--from` on needs no `--accept-change`.
- An OpenROAD whose `detailed_placement` has no `-use_diamond_legalizer` fails the run at setup.

The log shows each call's engine on its `legalizer` line, under `Detailed Placement Analysis`.

### Keep instances unchanged

`dont-touch` marks instances dont-touch in OpenROAD's sense, so repair neither resizes nor removes them, such as a buffer that forwards a clock to an output port:

```yaml
    dont-touch: [u_blk.u_clk_buf, "g_lane[*].u_fwd_buf"]
```

- Name each instance as the netlist spells it, as for `floorplan.macros`: brackets are literal, and `*` and `?` the only wildcards. An entry that matches nothing fails the floorplan, naming it.
- The flow marks them at the start of the floorplan, so the floorplan's setup repair and every later step see the mark. It is stored in the database, so checkpoints, later sessions and `--resume` keep it.
- OpenROAD's global connections skip a dont-touch instance (`ODB-0383`), and nothing connects its supplies later. The flow clears the mark just before the tap cells and the power grid, connects the supplies after them and marks the instances again, before the `after-pdn` hooks. When the block has global connection rules, such as the `add_global_connection` calls of a `pdn-config`, an instance left with a supply pin on no net then fails the floorplan, naming the pin. Without rules nothing is connected, so nothing is checked.
- A hook that marks cells itself with `set_dont_touch` should do it in `after-pdn` or later, for the same reason.
- OpenROAD does not reconnect a dont-touch instance's pins. `buffer_ports` leaves unbuffered an input port that drives one (`RSZ-0085`), `placement.macro-pin-limits` leaves a dont-touch macro's pins unbuffered and counts them unmet, and CTS stops with `ODB-0370` on one whose input is on a clock net it buffers ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#cts-stops-on-a-dont-touch-instance-whose-input-is-on-a-clock-net)).

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
