## Constrain boundary pins

`floorplan.pins` constrains IO pins in `pnr.yaml`. Each entry names ports by name or glob in `names`, and either constrains them to an edge, keeps them together, or places one pin exactly:

```yaml
    floorplan:
      pins:
        - names: [req_*, clk]          # the whole left edge
          side: left
        - names: rsp_*
          side: right
          start: 10                    # microns, die y from 10 to 80
          end: 80
        - names: pwdata                # a bus: every bit, together, in bit order
          side: top
          group: true
          order: true
        - names: irq
          location: [0, 42.5]          # pin centre, die microns
          layer: met3                  # optional
          size: [0.3, 0.6]             # optional, microns
```

| Key | Meaning |
| --- | --- |
| `names` | Port names or globs, one string or a list. A bus name matches every bit. |
| `side` | `left`, `right`, `top` or `bottom`: `set_io_pin_constraint -region <side>:<start>-<end>`. |
| `start` / `end` | Optional range along the side in die microns: y for `left` and `right`, x for `top` and `bottom`. Either one alone leaves the other end open. Without both the whole edge is allowed. |
| `group` / `order` | `group: true` keeps the matched pins next to each other (`-group`); `order: true` also keeps them in the order matched. With no `side` the group may land on any edge. |
| `location` | `[x, y]` pin centre in die microns. Places exactly one pin with `place_pin -force_to_die_boundary`, so it moves onto the nearest die edge for its layer's direction and onto the nearest routing track; the log reports the requested and the final centre. The pin is fixed, and pin placement leaves it there. |
| `layer` | With `location` only. Default: the PDK's horizontal pin layer on a left or right edge, its vertical one on a top or bottom edge, judged by the nearest edge. |
| `size` | With `location` only. `[width, height]` in microns. Default: the layer's minimum width and area. |

- Exact pins are placed first, then the edge and group constraints, then the `pin-constraints` file below, all just before `place_pins`. A pin both placed and matched by a side keeps its location.
- A name or glob that matches no port fails the run with `floorplan.pins[i]: no port matches '<pattern>'`, where `set_io_pin_constraint` alone would only warn. A `location` entry whose name matches more than one port fails too.
- A malformed entry fails when `pnr.yaml` loads, naming `floorplan.pins[i]`: an unknown side, `start` at or above `end`, a negative coordinate, `start` or `end` without `side`, `order` without `group`, `location` with `side`, `group` or more than one name, `layer` or `size` without `location`, or an entry that sets none of `side`, `group` and `location`.
- A `harden: true` run places its pins the same way, and the published abstract carries them. `floorplan.pins` is part of a hardened block's configuration, so changing it makes the block's abstract stale.
- Keep clock pins on layers inside the platform's clock-routing range. To keep a pin edge clear of macros, anchor the packer at the opposite corner with [`floorplan.macro-anchor`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls).

For anything `floorplan.pins` does not cover, such as `-mirrored_pins` or `exclude_io_pin_region`, set `pin-constraints: pins.tcl` on the run. The path is relative to `pnr.yaml` and a missing file fails the run. The file is sourced just before pin placement, after macro placement, the power grid and `floorplan.pins`:

```tcl
exclude_io_pin_region -region bottom:0-20
set_io_pin_constraint -mirrored_pins {a_in a_out}
```

Put these commands here, not in SDC, which is read before the die exists.
