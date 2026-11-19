## Place macros

Hard macros (SRAMs, hardened blocks) are placed before the power grid by OpenROAD's RTL-MP (`rtl_macro_placer`), as in ORFS. It needs OpenROAD 26Q4 or newer; on an older one a run with macros stops at macro placement saying so.

- RTL-MP places by connectivity and may mirror a macro. It reports to `rtlmp/`, and `pnr.log` has one `placed` line per macro.
- rb passes ORFS's arguments: `placement.density` as `-target_util`, and `placement.macro-channel` as `-min_channel_size`, the minimum channel in µm between macros and from the core edge. RTL-MP trims each channel to the pins along it unless `placement.macro-pin-aware-channels: false`.
- ORFS sets a channel on every platform, such as `[80, 80]` on sky130hd. rb sets none unless configured, and RTL-MP's own channels can leave none. Set one when the grid fails with `PDN-0179` or global routing with `GRT-0116`.
- To place macros yourself, locate them with `floorplan.macros` or place them in a [`macro-placement` hook](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#run-tcl-hooks), which replaces RTL-MP. Either way, no macro may overlap a core cut-out or [`hard` blockage](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#shape-the-floorplan).

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

- A `location` is snapped to the site and row grid, and must be inside the core, clear of other fixed macros, and match one macro. RTL-MP places the rest around it, and does not run when every macro is located.
- An `orientation` needs a `location`, since RTL-MP orients the macros it places, unless a `macro-placement` hook places them.
- A pattern that matches nothing fails the run.
- A turned macro (`R90`, `R270`, `MXR90`, `MYR90`) turns its pins and power pins. Give the `pdn-config` a macro grid for that orientation, or `pdngen` fails with `PDN-0233`. `placement.rotated-macro-footprint` works around OpenROAD's legalizer for turned macros ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#openroads-legalizer-blocks-a-rotated-macros-unrotated-footprint)).
- `floorplan.macros-file` takes a floorplanner's YAML or JSON list of the same entries. `macro-snap: false` keeps exact coordinates.
