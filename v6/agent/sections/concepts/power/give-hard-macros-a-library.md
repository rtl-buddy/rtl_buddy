## Give hard macros a library

The `platform` corner characterises standard cells only. A hard macro such as an SRAM, PLL or hardened partition needs its own Liberty. Without one, the macro stays in `power_instances.rpt` but contributes exactly zero:

```text
Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
```

The power run inherits the libraries of the run it references, so the usual case needs no configuration:

| `netlist-source` | Inherited from the upstream entry |
| --- | --- |
| `pnr` | `lib-paths` |
| `synth` | `lib-paths` and `lef-paths` |

A hardened block named under [`blocks:`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#assemble-hardened-blocks) is resolved too. A block with no abstract fails the run before OpenROAD starts and names the block. Do not add the abstract's `.lib` to `lib-paths`: it has timing arcs but no power tables, and loading it makes OpenROAD ignore SAIF, VCD and `set_power_activity` values on the block's outputs. Without it the block reports 0 W and appears in the [unpowered-cell warning](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#read-the-unpowered-cell-warning).

To add a library only the analysis needs, such as a corner the upstream run was not routed against, set `lib-paths` on the `power.yaml` entry:

```yaml
runs:
  - name: demo_power_macro
    netlist-source: pnr
    pnr: demo_pnr
    pnr-path: ../../pnr/demo/pnr.yaml
    platform: sky130hd_tt
    lib-paths: ["../../pdk/sram/sram_TT_1p8V_25C.lib"]
```

Paths resolve from `power.yaml`.
