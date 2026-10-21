## Place macros

A netlist with hard macros (SRAMs, hardened blocks) gets them placed before the power grid. By default rb's packer puts them in rows from the core's lower-left corner, tallest first, `placement.macro-halo` (default 20 µm) apart and from the core edge, on the site grid. `floorplan.macro-anchor` starts from another corner, keeping the opposite edges free for pins. `floorplan.macro-placement: rtl-mp` uses OpenROAD's RTL-MP instead, which places by connectivity and may mirror macros; it keeps out of every blockage type and cannot take a `macro-anchor`.

`floorplan.macros` fixes, orients or haloes individual macros:

```yaml
    floorplan:
      macros:
        - instance: u_core/u_sram          # instance name or glob (* crosses /)
          location: [200, 290]             # lower-left corner after orientation, die µm
          orientation: MX
          halo: [3, 3]                     # replaces macro-cell-halo for this macro
        - instance: "*/u_rf*"
          orientation: R180
```

- A `location` is snapped to the site and row grid, must be inside the core and clear of other fixed macros, and must match one macro. Placers leave it there.
- Under `rtl-mp`, an `orientation` needs a `location`.
- A pattern that matches nothing fails the run.
- A turned macro (`R90`, `R270`, `MXR90`, `MYR90`) turns its pins and power pins. Give the `pdn-config` a macro grid for that orientation, or `pdngen` fails (`PDN-0233`). `placement.rotated-macro-footprint` works around OpenROAD's legalizer for turned macros; see [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v7/known-issues/#openroads-legalizer-blocks-a-rotated-macros-unrotated-footprint).

A floorplanner can hand over macros as a YAML or JSON list of the same entries in `floorplan.macros-file`. `macro-snap: false` keeps its exact coordinates.
