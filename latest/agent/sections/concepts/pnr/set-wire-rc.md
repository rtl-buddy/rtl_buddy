## Set wire RC

Set the PDK's `layer-rc-tcl` so clock-tree synthesis, placement parasitics and hold repair see wire resistance and capacitance. Without it they see only the technology LEF's layer RC, which is zero on most open PDKs. CTS then balances, and hold repair fixes, a design with no wires, and the routed timing can fail hold that repair never saw.

`rb pnr` warns with `pnr.no_wire_rc` when the PDK sets no `layer-rc-tcl`, or when `pnr.log` has OpenROAD's `[WARNING EST-0018]` (zero wire capacitance) or `[WARNING CTS-0104]` (zero clock wire RC). The warning is logged and appended to a passing run's description, naming the cause:

```text
P&R passed; pnr.no_wire_rc: pre-route steps saw no wire RC (no layer-rc-tcl, EST-0018, CTS-0104); see rb docs show concepts/pnr#set-wire-rc
```

For sky130hd, copy the `set_layer_rc` lines of OpenROAD's `test/sky130hd/sky130hd.rc` and add the wire RC layers for signal and clock nets:

```tcl
# pdk/sky130hd/setRC.tcl, named by cfg-pdks layer-rc-tcl
set_layer_rc -layer met1 -capacitance 1.72375E-04 -resistance 8.929e-04
# ... one line per layer and via, from sky130hd.rc
set_wire_rc -signal -layer met2
set_wire_rc -clock -layer met5
```

To also catch hold growth from wire lengths before detail route, set `global-route-hold-repair: true` on the platform. It runs `repair_timing -hold` on the global-route estimate, then legalizes the added buffers and reroutes their nets incrementally. It is off by default because it changes QoR. A `global_route` [checkpoint](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#keep-stage-checkpoints) holds the route before this repair.
