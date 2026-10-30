## Shape the floorplan

The die is sized from the netlist's cell area by `floorplan.utilization`, which has no default, `aspect` (default 1.0) and `core-margin` (default 1.0 µm). To fix it exactly, set `die-area` and `core-area` together (`[x0, y0, x1, y1]` in µm); they exclude the other three. `core-cutouts` carves rectangles out of the core for an L- or T-shaped core: they block cells, rows, taps and macros, not routing, and the die stays rectangular. With utilization sizing, lower `utilization` to leave room for cut-outs.

`floorplan.blockages` keep standard cells out of rectangles (die µm):

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

After macro placement, and again at the end of the floorplan when an `after-pdn`, `after-pins` or `stage-end` hook is set, a macro that overlaps a `hard` blockage or a core cut-out fails the run, naming the macro, the region and the ways out: a `floorplan.macros` location, a larger `placement.macro-channel`, or moving the blockage. RTL-MP treats every blockage as an obstacle and stops with `MPL-0065` or `MPL-0040` when it finds no placement clear of them, but rb checks only `hard` blockages and cut-outs. `lifetime: global-placement` makes any type a screen for global placement alone: no rows are cut, macro placement ignores it and no checkpoint holds it. Blockages need OpenROAD 26Q1.
