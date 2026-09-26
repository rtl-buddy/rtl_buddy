## Constrain boundary pins

Set `pin-constraints: pins.tcl` in a run to assign pins to boundary edges or
groups. The path is relative to `pnr.yaml`; it is sourced immediately before
`place_pins`. Missing files fail. Without this key the default unconstrained
placement is unchanged.

IO pins are placed after [macro placement](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#macro-placement) and the power
grid, and before global placement — the order OpenROAD's reference flow uses.
Placing them first left every macro unplaced at that point: each drew a
`PPL-0015 Macro ... is not placed` warning, and the pin placer's
wirelength-driven assignment saw the macro at the origin instead of where its
pins are. `place_pins` does not, however, keep pins off the stretch of an edge
a macro sits behind: it excludes only boundary intervals a placed macro
actually touches, and the packer keeps every macro a halo inside the core. To
keep a pin edge clear of macros, anchor the packer at the opposite corner with
[`floorplan.macro-anchor`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls).

For example, `pins.tcl` can contain:

```tcl
set_io_pin_constraint -pin_names {req_* clk} -region left:*
set_io_pin_constraint -pin_names {rsp_*} -region right:*
```

Do not put these commands in SDC: SDC is read before the floorplan exists,
when whole-edge intervals can resolve to zero length. Keep clock pin layers
compatible with the platform's clock-routing range.
