## Size the die and core

By default `utilization`, `aspect` and `core-margin` size the floorplan from the netlist's cell area. To give exact dimensions instead, set both rectangles in die microns:

```yaml
    floorplan:
      die-area: [0, 0, 160, 160]       # x0 y0 x1 y1
      core-area: [20, 20, 140, 140]
      core-cutouts:
        - [80, 80, 140, 140]           # an L-shaped core
```

- `die-area` and `core-area` go together and run `initialize_floorplan -die_area -core_area`. They exclude `utilization`, `aspect` and `core-margin`; setting any of those with them is a load error. The core must lie inside the die. OpenROAD snaps the core edges inward onto the site grid and logs `IFP-0028`.
- `core-cutouts` carves rectangles out of the core for an L, T or other rectilinear core. Each is a hard placement blockage whose rows are cut before tap insertion, like a `hard` [blockage](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls), and a keep-out for the macro packer. With `core-area` set, a cut-out must lie inside it; at run time, one that does not overlap the core fails the run. It works with utilization sizing too, but the core is still sized for the whole cell area, so lower `utilization` to leave room.
- The die stays rectangular: OpenROAD has no non-rectangular die. A cut-out keeps standard cells, rows, taps, followpin rails and macros out; it does not block routing. Core-grid straps still cross it and stay connected through the rows on either side, so `check_power_grid` passes. IO pins are still placed along the whole die edge; keep them off the edges next to a cut-out with [`floorplan.pins`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#constrain-boundary-pins).
- All three keys are part of a hardened block's configuration when set.
