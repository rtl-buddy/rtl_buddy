## Place macros

A netlist with hard macros (SRAMs, hardened blocks) gets them placed before the power grid by OpenROAD's RTL-MP (`rtl_macro_placer`), as ORFS places them. RTL-MP places by connectivity and may mirror a macro. It reports to `rtlmp/` in the run directory, and `pnr.log` has one `placed` line per macro it places. rb passes it the arguments ORFS does: `-target_util` is `placement.density`, and `placement.macro-channel` is its minimum channel in µm between macros and from the core edge (`-min_channel_size`), which it trims to the macro pins along each channel unless `placement.macro-pin-aware-channels` is `false`. ORFS sets a channel on every platform, such as `[80, 80]` on sky130hd; rb sets none unless configured, and RTL-MP then keeps its own channels, which can leave none. A channel too narrow for the power grid fails `pdngen` with `PDN-0179`, and one too narrow for routing fails global routing with `GRT-0116`; set `placement.macro-channel` then. It needs OpenROAD 26Q4 or newer; on an older one, a run with macros stops at macro placement saying so. To place macros another way, fix them with `floorplan.macros` locations, or place them in a [`macro-placement` hook](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#run-tcl-hooks), which replaces RTL-MP. Whichever places them, no macro may overlap a core cut-out or a `hard` [blockage](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#shape-the-floorplan); the run fails otherwise.

`floorplan.macros` fixes, orients or haloes individual macros:

```yaml
    floorplan:
      macros:
        - instance: u_core/u_sram          # instance name or glob (* crosses /)
          location: [200, 290]             # lower-left corner after orientation, die µm
          orientation: MX
          halo: [3, 3]                     # replaces macro-cell-halo for this macro
        - instance: "*/u_rf*"
          halo: [2, 2]                     # every macro the glob matches
```

- A `location` is snapped to the site and row grid, must be inside the core and clear of other fixed macros, and must match one macro. RTL-MP places the other macros around it, and does not run when every macro is located.
- An `orientation` needs a `location`, because RTL-MP chooses the orientation of each macro it places. With a `macro-placement` hook, an entry may orient a macro for the hook to place.
- A pattern that matches nothing fails the run.
- A turned macro (`R90`, `R270`, `MXR90`, `MYR90`) turns its pins and power pins. Give the `pdn-config` a macro grid for that orientation, or `pdngen` fails (`PDN-0233`). `placement.rotated-macro-footprint` works around OpenROAD's legalizer for turned macros; see [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#openroads-legalizer-blocks-a-rotated-macros-unrotated-footprint).

A floorplanner can hand over macros as a YAML or JSON list of the same entries in `floorplan.macros-file`. `macro-snap: false` keeps its exact coordinates.
