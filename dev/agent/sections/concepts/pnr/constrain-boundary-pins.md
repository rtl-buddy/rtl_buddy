## Constrain boundary pins

Set `pin-constraints: pins.tcl` in a run to assign pins to boundary edges or
groups. The path is relative to `pnr.yaml`; it is sourced after floorplan and
track initialization, immediately before `place_pins`. Missing files fail.
Without this key the default unconstrained placement is unchanged.

For example, `pins.tcl` can contain:

```tcl
set_io_pin_constraint -pin_names {req_* clk} -region left:*
set_io_pin_constraint -pin_names {rsp_*} -region right:*
```

Do not put these commands in SDC: SDC is read before the floorplan exists,
when whole-edge intervals can resolve to zero length. Keep clock pin layers
compatible with the platform's clock-routing range.
