## Give hard macros a library

The `platform` corner characterises the standard cells and nothing else. A hard macro — an SRAM, a PLL, a hardened partition — gets its Liberty from the run that placed it, through that run's `lib-paths`, and a power analysis that read only the platform corner had no library for it at all. The macro was still in the design and still in `power_instances.rpt`, contributing exactly zero:

```text
Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
```

The power run inherits those libraries from the run it references, so the ordinary case needs no new configuration:

| `netlist-source` | Inherited | Why |
| --- | --- | --- |
| `pnr` | the P&R entry's `lib-paths` | The routed ODB already carries every master the router placed, so no LEF is read |
| `synth` | the synthesis entry's `lib-paths` and `lef-paths` | `read_verilog` and `link_design` build the database out of LEF masters |

A hardened block the upstream entry names under [`blocks:`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#assemble-hardened-blocks) is resolved too. On the `synth` side its abstract's `.lef` is read after the `lef-paths`, so `link_design` has a master for it. The abstract is resolved without the staleness check `rb pnr` applies, because the analysis reads what was routed. A block with no abstract fails the run before OpenROAD starts, naming the block. The result's `blocks` field lists the abstracts used.

The abstract's `.lib` is **not** read. It comes from `write_timing_model`: it has timing arcs and no power tables, and its output pins have no `function`. Loaded, it makes every block output the end of a clock-to-out arc, and OpenSTA's activity propagation, which starts from the design's inputs and never from a clock pin, does not reach those outputs. They read zero activity. A SAIF, VCD or `set_power_activity` value on them is ignored as well, so the parent logic the block drives reads as static. Without the `.lib`, a block output is a source like a primary input: it takes the trace's activity, or the default toggle rate and duty. Either way the block reports 0 W and is named in the unpowered-cell warning below. Don't add an abstract's `.lib` to the entry's `lib-paths` either, for the same reason.

`lib-paths` on the `power.yaml` entry adds to the inherited list, for a library only the analysis needs — a corner the upstream run was not routed against, say:

```yaml
runs:
  - name: demo_power_macro
    netlist-source: pnr
    pnr: demo_pnr
    pnr-path: ../../pnr/demo/pnr.yaml
    platform: sky130hd_tt
    lib-paths: ["../../pdk/sram/sram_TT_1p8V_25C.lib"]
```

Paths resolve from `power.yaml`, as every other path on the entry does. The inherited libraries are read first and the run's own after them, so a macro library never shadows a standard cell; a library named on both sides is read once. A configured file that is not on disk fails the run before OpenROAD is launched, rather than leaving a diagnostic in `power.log` and the macro back at zero.
