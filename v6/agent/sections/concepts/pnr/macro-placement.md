## Macro placement

A netlist that instantiates hard macros, such as an SRAM or a partition hardened by an earlier run, gets automatic macro placement before the power grid is built. To use OpenROAD's placer instead, see [RTL-MP macro placement](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#rtl-mp-macro-placement).

- The packer sorts macros tallest first, then widest, and packs them left to right into rows starting at the core's lower-left corner, or at the corner [`floorplan.macro-anchor`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#floorplan-controls) names.
- It keeps `placement.macro-halo` between neighbours and at every core edge, and snaps each origin to the standard-cell site grid.
- Placed macros are `FIRM`; global and detailed placement leave them where they are.
- To fix, orient or halo a single macro, see [Place individual macros](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#place-individual-macros).

When macros do not fit, the run fails with `N macros do not fit the W x H um floorplan core`, followed by the largest footprint, the macro and core areas, and the smallest core at the run's aspect ratio that would fit. Lower the floorplan utilization, change its aspect ratio, or lower `placement.macro-halo`.
