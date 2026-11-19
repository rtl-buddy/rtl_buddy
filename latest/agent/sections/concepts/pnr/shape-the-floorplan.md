## Shape the floorplan

The floorplan sets the die and core and keeps cells out of chosen areas.

- `floorplan.utilization` (no default), `aspect` (default 1.0) and `core-margin` (default 1.0 µm) size the die from the netlist's cell area.
- `die-area` and `core-area`, set together as `[x0, y0, x1, y1]` µm, fix it exactly and exclude those three.
- `core-cutouts` carves rectangles out of the core for an L- or T-shaped core. They block cells, rows, taps and macros, not routing; the die stays rectangular. Lower `utilization` to leave room for them.

`floorplan.blockages` keep standard cells out of rectangles (die µm) and need OpenROAD 26Q1:

```yaml
    floorplan:
      blockages:
        - rect: [10, 10, 130, 60]                 # hard: no cells, rows cut
        - rect: [400, 300, 520, 420]
          type: partial                            # global placement caps density here
          max-density: 0.4
        - rect: [200, 50, 260, 120]
          type: soft                               # global placement only; repair may use it
          lifetime: global-placement               # removed after global placement
```

- `lifetime: global-placement` makes any type a screen for global placement alone: no rows are cut, macro placement ignores it, and no checkpoint holds it.
- A macro overlapping a `hard` blockage or core cut-out fails the run, naming the macro, the region and the fixes: a `floorplan.macros` location, a larger `placement.macro-channel`, or moving the blockage. rb checks after macro placement, and again at the end of the floorplan when an `after-pdn`, `after-pins` or `stage-end` hook is set.
- RTL-MP treats every blockage as an obstacle and stops with `MPL-0065` or `MPL-0040` when it finds no clear placement. rb checks only `hard` blockages and cut-outs.
