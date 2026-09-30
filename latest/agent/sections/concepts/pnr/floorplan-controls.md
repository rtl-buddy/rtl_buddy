## Floorplan controls

Two optional `floorplan` keys steer where macros and standard cells may go.

```yaml
    floorplan:
      utilization: 0.45
      macro-anchor: upper-right        # default lower-left
      blockages:
        - rect: [10, 10, 130, 60]      # microns, die coordinates: x0 y0 x1 y1
        - rect: [400, 300, 520, 420]
          type: partial
          max-density: 0.4
```

**Macro anchor.** `macro-anchor` is the core corner the [macro packer](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#macro-placement) starts from: `lower-left` (default), `lower-right`, `upper-left` or `upper-right`. Rows fill away from that corner, so the opposite edges stay clear of macros where possible. Point `pin-constraints` at those edges when a neighbouring block abuts one side. Set it per run, not per platform.

**Placement blockages.** Each `blockages` entry is `rect: [x0, y0, x1, y1]` in die microns plus a `type`:

| `type` | Effect |
| --- | --- |
| `hard` (default) | No standard cell is placed inside. The packer moves overlapping macros past it. |
| `soft` | Global placement keeps cells out; later repair and legalization may use the area. Macros may sit on it, except under `rtl-mp`. |
| `partial` | Global placement caps cell density at `max-density` (strictly between 0 and 1); legalization then treats the area as fully blocked. Macros may sit on it, except under `rtl-mp`. |

Blockages need OpenROAD 26Q1 or newer; on an older build the run fails at setup. A malformed rectangle (`x0 >= x1`, `y0 >= y1`, a side under 0.001 µm, a negative coordinate, or not four numbers), an unknown type, or `max-density` on anything but `partial` fails when `pnr.yaml` loads.
