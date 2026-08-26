## Choose the design source

| Source | Input | Timing and parasitics | Required upstream runs |
| --- | --- | --- | --- |
| `netlist-source: synth` | `synth_netlist.v` | User SDC, no wire parasitics or clock tree | `rb synth` |
| `netlist-source: pnr` | `<top>.routed.odb` | Routed SDC, CTS, and global-routing parasitic estimates | `rb synth`, then `rb pnr` |

The default `synth` source is useful for early leakage and activity comparisons but underestimates switching because it has no routed wire capacitance. Use `pnr` for a more representative post-route estimate.

If the routed ODB is missing, rerun `rb pnr`.
