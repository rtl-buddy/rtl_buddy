## Choose the design source

| Source | Input | Timing and parasitics | Required upstream runs |
| --- | --- | --- | --- |
| `netlist-source: synth` (default) | `synth_netlist.v` | User SDC, no wire parasitics or clock tree | `rb synth` |
| `netlist-source: pnr` | `<top>.routed.odb` | Routed SDC, CTS, and parasitics | `rb synth`, then `rb pnr` |

The `synth` source suits early leakage and activity comparisons but underestimates switching, which lacks wire capacitance. Use `pnr` for a post-route estimate; if the routed ODB is missing, rerun `rb pnr`.
