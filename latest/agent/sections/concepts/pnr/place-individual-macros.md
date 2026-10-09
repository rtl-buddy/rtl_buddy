## Place individual macros

`floorplan.macros` gives individual macros a fixed location, an orientation or their own standard-cell halo. Each entry names an `instance`, by its full hierarchical name or a glob over the names:

```yaml
    floorplan:
      macros:
        - instance: u_inner/u_hist_mem/u_sram
          location: [200, 290]         # lower-left corner, die microns
          orientation: MX
          halo: [3, 3]                 # standard-cell keep-out, x and y, microns
        - instance: "*/u_c*"           # every matching macro
          orientation: R180
```

| Key | Meaning |
| --- | --- |
| `instance` | A macro instance name as OpenROAD reads it from the netlist, such as `u_inner/u_hist_mem/u_sram`. A name that is not an instance is matched as a glob, where `*` also crosses `/`. |
| `location` | `[x, y]`, the macro's lower-left corner in die microns, after orientation. The macro is fixed (`LOCKED`) before the packer or RTL-MP runs, and both leave it there. The pattern must match exactly one macro. |
| `orientation` | `R0`, `R180`, `MX` or `MY`. Applied before placement, so a packed macro keeps it. |
| `halo` | `[x, y]` in microns. Replaces `placement.macro-cell-halo` for the matched macros: no standard cell is placed within `x` of the macro's left and right edges or `y` of its top and bottom. `[0, 0]` removes the halo. |

- A location snaps to the nearest standard-cell site and row from the core's lower-left corner, the grid the packer also uses, so the macro's bottom edge lies on a row boundary. A macro that fits the core before snapping still fits after. The log shows `fixed <inst> at (x, y) um <orientation> (requested (x, y))`.
- A fixed macro must lie inside the core and must not overlap another fixed macro; either fails the run naming the entry. Overlap with a placement blockage is allowed.
- With the [packer](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#macro-placement), fixed macros are keep-outs: the other macros stay `placement.macro-halo` away from them, as from each other. With `rtl-mp`, RTL-MP treats them as fixed obstacles.
- Rotations by 90 degrees (`R90`, `R270`, `MXR90`, `MYR90`) are refused: they swap the macro's width and height off the site grid and turn its pins across the routing tracks. Under `rtl-mp`, an `orientation` without a `location` is refused, because RTL-MP picks each placed macro's orientation itself.
- A macro may be matched by several entries, but only one entry may set each key for it.
- At run time a pattern that matches no instance, or that matches a standard cell, fails the run: `floorplan.macros[i]: no instance matches '<pattern>'`.
- `floorplan.macros` is part of a hardened block's configuration. For a [hardened block](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#assemble-hardened-blocks) placed with a `location`, check that its supply pins land on the parent's straps, and that a mirrored block's net order still fits the parent's grid.
