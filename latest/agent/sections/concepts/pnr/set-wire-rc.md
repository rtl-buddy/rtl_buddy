## Set wire RC

Set the PDK's `layer-rc-tcl`. Without it, placement, clock-tree synthesis and hold repair see the technology LEF's layer RC, which is zero on most open PDKs, and the routed design can fail hold that repair never saw. rb warns `pnr.no_wire_rc` and qualifies the `PASS` when the PDK sets no `layer-rc-tcl` or `pnr.log` has `EST-0018` (zero wire capacitance) or `CTS-0104` (zero clock wire RC).

For sky130hd, copy the `set_layer_rc` lines of OpenROAD's `test/sky130hd/sky130hd.rc` and pick the wire layers:

```tcl
set_layer_rc -layer met1 -capacitance 1.72375E-04 -resistance 8.929e-04
# ... one line per layer and via
set_wire_rc -signal -layer met2
set_wire_rc -clock -layer met5
```

To catch hold problems from wire length before detailed routing, also set `global-route-hold-repair: true` on the platform. See [Repair timing after routing](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/pnr/#repair-timing-after-routing).
