## Tune placement, clock tree and repair

Process-dependent knobs live on the PDK, can be overridden on the platform, and most also on a run, so runs of one block on one platform can differ without a platform per variant. Which level takes each key is in the [YAML reference](https://rtl-buddy.github.io/rtl_buddy/v7/reference/yaml/#synthesis-and-physical-design-tools). Defaults suit Nangate45. Start from the problem:

| Problem | Knob |
| --- | --- |
| Global route overflows | Lower `placement.density` (default 0.7) or `floorplan.utilization`; `placement.routability-driven: true` (add `routability-use-grt: true` for a slower, closer estimate); a fixed `placement.reference-hpwl` such as `446000000` spreads a large design further. |
| DRCs left in congested areas | `routing-layer-adjustment` (0 to 1) withholds that fraction of each signal layer from the global router; `global-route.layer-adjustment` sets it per layer. See [Tune global routing](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/pnr/#tune-global-routing). |
| Setup misses on long nets | `placement.timing-driven: true`. On OpenROAD 26Q4 it also buffers and resizes during placement, which grows area. Needs wire RC. |
| Setup misses after CTS | `post-cts-setup-repair: true` |
| Timing misses after routing | [`global-route-repair` or `post-route-repair`](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/pnr/#repair-timing-after-routing) |
| Many fanout buffers on a library without a fanout limit (sky130hd) | `max-fanout: 100000` stops buffering for fanout alone; `repair_design` otherwise uses 50. |
| Cells too tight | `placement.padding` (sites, default 1) |
| Clock tree | `cts-buffer` (a list's first entry is the root buffer), `cts-sink-clustering`, `cts-apply-ndr`; `cts-clock-nets` builds trees for the named clocks only |
| Setup misses from flops on a clock branch that CTS padded to match the deepest one (`CTS-0033` and `CTS-0036` in the log), as when a clock drives a buffer as well as flops | `cts-latency-balancing: false` |
| Long clock wires after CTS | `cts-repair-clock-nets: true` |
| A block must leave the top layers to its parent | `routing-layers` on the run |
| A block needs other tap spacing or endcaps than the PDK's | `tapcell-tcl` on the platform or the run |
| Cells crowd macro edges, or Metal Spacing DRCs at a block edge | `placement.macro-cell-halo` (µm, default 1.0) |
| Tie cells too close to their loads | `placement.tie-separation` (µm, default 0) |
| Macro pins over a tight Liberty limit, such as OpenRAM SKY130 SRAM inputs (0.04 ns) | `placement.macro-pin-limits`, below |

A platform that ends up with `routability-use-grt` but not `routability-driven` fails to load.

On a library with several threshold-voltage flavours, such as ASAP7 RVT and LVT with both flavours' Liberty in each corner and both cell LEFs in `macro-lef`, resizing moves cells between flavours freely. `keep-sizing-vt: true` keeps each cell's flavour when `repair_design` or `repair_timing` resizes it, so a cell changes flavour only through timing repair's threshold-voltage swap. `cts-dont-use-cells` keeps cells out of the clock tree only: CTS and `repair_clock_nets` skip them, and post-CTS repair may still place them on data paths. After `repair_clock_nets` the flow frees only the cells the list made dont-use; a matching cell that Liberty `dont_use`, `dont-use-cells` or an earlier hook excluded stays excluded. Neither CTS nor `repair_clock_nets` checks the `cts-buffer` cells against either list. The list governs the cells CTS picks itself: dummy-load inverters, and delay buffers it infers. Name the buffer flavour in `cts-buffer`. To keep a flavour out of every step, list it in `dont-use-cells`.

`repair_design` sizes repeaters for the library's target slew, so a macro pin whose own limit is far below it stays in `electrical.rpt`. `placement.macro-pin-limits` buffers such pins at the end of the place stage, after `repair_design`, the `after-repair-design` hook and legalization:

```yaml
    placement:
      macro-pin-limits:
        buffers: [sky130_fd_sc_hd__buf_1, sky130_fd_sc_hd__buf_2, sky130_fd_sc_hd__buf_4,
                  sky130_fd_sc_hd__buf_8, sky130_fd_sc_hd__buf_12, sky130_fd_sc_hd__buf_16]
        margin: 0.05              # of each max-slew limit, kept in hand
        capacitance-margin: 0.5   # of each max-capacitance limit; default: margin
```

Each macro data pin over its limit less the margin gets a buffer fixed on the free row site nearest it (an input also gets a pre-driver), sized from `buffers` weakest first and marked `dont_touch`, so later repair leaves it alone. `pnr.log` has one `>>>` line per pin. The step judges on placement parasitics, so check `electrical.rpt`: a long macro-output net can route to twice its estimated capacitance (hence `capacitance-margin: 0.5` on sky130hd), a large `macro-cell-halo` keeps the buffer too far from the pin (3 µm works with `buf_8` on sky130hd, 20 µm does not), and a pin route that global routing detours around the macro is out of its reach. A block set on the run or platform replaces a lower level's whole.
