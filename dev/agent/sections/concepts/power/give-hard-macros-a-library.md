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
