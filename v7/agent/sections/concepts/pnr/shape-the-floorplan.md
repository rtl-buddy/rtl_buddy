## Shape the floorplan

By default the die is sized from the netlist's cell area by `floorplan.utilization`, `aspect` and `core-margin`. To fix it exactly, set `die-area` and `core-area` together (`[x0, y0, x1, y1]` in µm); they exclude the other three. `core-cutouts` carves rectangles out of the core for an L- or T-shaped core: they block cells, rows, taps and macros, not routing, and the die stays rectangular. With utilization sizing, lower `utilization` to leave room for cut-outs.

`floorplan.blockages` keep standard cells out of rectangles (die µm):

```yaml
    floorplan:
      blockages:
        - rect: [10, 10, 130, 60]                 # hard: no cells, rows cut, macros moved past it
        - rect: [400, 300, 520, 420]
          type: partial                            # global placement caps density here
          max-density: 0.4
        - rect: [200, 50, 260, 120]
          type: soft                               # global placement only; repair may use it
          lifetime: global-placement               # removed after global placement
```

`lifetime: global-placement` makes any type a screen for global placement alone: no rows are cut, macro placement ignores it and no checkpoint holds it. Blockages need OpenROAD 26Q1.
