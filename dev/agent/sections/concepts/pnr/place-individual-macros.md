## Place individual macros

`floorplan.macros` gives individual macros a fixed location, an orientation or their own standard-cell halo. Each entry names an `instance`, by its full hierarchical name or a glob over the names. A floorplanner's output can come from a [file](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#read-macros-and-pins-from-planner-files) instead:

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
| `location` | `[x, y]`, the lower-left corner of the macro's box after orientation, in die microns. The macro is fixed (`LOCKED`) before the packer or RTL-MP runs, and both leave it there. The pattern must match exactly one macro. |
| `orientation` | `R0`, `R90`, `R180`, `R270`, `MX`, `MY`, `MXR90` or `MYR90`. Applied before any location and before placement, so a packed macro keeps it. See [Rotate a macro](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#rotate-a-macro). |
| `halo` | `[x, y]` in microns. Replaces `placement.macro-cell-halo` for the matched macros: no standard cell is placed within `x` of the macro's left and right edges or `y` of its top and bottom. `[0, 0]` removes the halo. |

- A location snaps to the nearest standard-cell site and row from the core's lower-left corner, the grid the packer also uses, so the macro's bottom edge lies on a row boundary. A macro that fits the core before snapping still fits after. The log shows `fixed <inst> at (x, y) um <orientation> (requested (x, y))`.
- `floorplan.macro-snap: false` (default `true`) places every located macro at its exact location, rounded once to the database unit, off the site and row grid. The location is then the floorplanner's to keep on the grids the flow relies on: a macro whose bottom edge lies between two rows can fail legalization with `DPL-0011`.
- A fixed macro must lie inside the core and must not overlap another fixed macro; either fails the run naming the entry. Overlap with a placement blockage is allowed.
- With the [packer](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#macro-placement), fixed macros are keep-outs: the other macros stay `placement.macro-halo` away from them, as from each other. With `rtl-mp`, RTL-MP treats them as fixed obstacles.
- Under `rtl-mp`, an `orientation` without a `location` is refused, because RTL-MP picks each placed macro's orientation itself.
- A macro may be matched by several entries, inline or from the file, but only one entry may set each key for it.
- At run time a pattern that matches no instance, or that matches a standard cell, fails the run: `floorplan.macros[i]: no instance matches '<pattern>'`.
- A `location` on a netlist with no hard macros fails the run at macro placement, naming up to three located entries: `floorplan.macros: the netlist has no hard macros, but 1 entry gives a macro location: floorplan.macros[0] (u_sram)`.
- `floorplan.macros` is part of a hardened block's configuration. For a [hardened block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#assemble-hardened-blocks) placed with a `location`, check that its supply pins land on the parent's straps, and that a mirrored block's net order still fits the parent's grid.
