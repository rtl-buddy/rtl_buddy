## Constrain boundary pins

Set `pin-constraints: pins.tcl` on a run to assign pins to boundary edges or groups. The path is relative to `pnr.yaml` and a missing file fails the run. Without the key, pin placement is unconstrained.

```tcl
set_io_pin_constraint -pin_names {req_* clk} -region left:*
set_io_pin_constraint -pin_names {rsp_*} -region right:*
```

- Put these commands here, not in SDC. The file is sourced just before pin placement, after macro placement and the power grid.
- Keep clock pin layers compatible with the platform's clock-routing range.
- To keep a pin edge clear of macros, anchor the packer at the opposite corner with [`floorplan.macro-anchor`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls).
