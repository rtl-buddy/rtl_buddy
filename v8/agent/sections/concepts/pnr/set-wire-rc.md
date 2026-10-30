## Set wire RC

Set the PDK's `layer-rc-tcl`. Without it, placement, clock-tree synthesis and hold repair see the technology LEF's layer RC, which is zero on most open PDKs, and the routed design can fail hold that repair never saw. Setup repair cannot run on zero wire RC: OpenROAD stops with `RSZ-0089`, and the failed run's description names the cause and both fixes. Three steps repair setup by default, as ORFS does: `floorplan-setup-repair`, `post-cts-setup-repair` and the setup repair of `global-route-repair`. Without wire RC, all three must be off: `floorplan-setup-repair: false`, `post-cts-setup-repair: false`, and a `global-route-repair` without `setup-margin`, such as `{hold-margin: 0}`, or `null`. rb warns `pnr.no_wire_rc` and qualifies the `PASS` when the PDK sets no `layer-rc-tcl` or `pnr.log` has `EST-0018` (zero wire capacitance) or `CTS-0104` (zero clock wire RC). Without `layer-rc-tcl`, `session` hooks may set the wire RC instead: each session then checks after the hooks that every corner has signal and clock wire capacitance, and the run is not qualified for the missing `layer-rc-tcl` when every check passes.

For sky130hd, copy the `set_layer_rc` lines of OpenROAD's `test/sky130hd/sky130hd.rc` and pick the wire layers:

```tcl
set_layer_rc -layer met1 -capacitance 1.72375E-04 -resistance 8.929e-04
# ... one line per layer and via
set_wire_rc -signal -layer met2
set_wire_rc -clock -layer met5
```

The default [global-route repair](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#repair-timing-after-routing) repairs again on the global-route estimate, which sees the wire length that placement estimates miss.
