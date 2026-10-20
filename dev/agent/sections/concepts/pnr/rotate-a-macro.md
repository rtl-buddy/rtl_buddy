## Rotate a macro

`R90`, `R270`, `MXR90` and `MYR90` turn a macro by 90 degrees, so its box is the master's height wide and its width tall. `location` is the lower-left corner of that turned box, and the packer packs a turned macro by its turned size. RTL-MP does not turn macros.

Turning a macro turns its pins and power ring with it, so pin access and power are the floorplan's to check: horizontal pin shapes become vertical, and a PDK power grid written for unrotated macros may have no grid for the turned orientations or connect layers that no longer cross. `pdngen` then fails, for example with `PDN-0233` when a macro grid gets no via. Give the PDK's `pdn-config`, or the run's own, a macro grid for the turned orientations that reaches the turned power pins.

OpenROAD's default legalizer blocks a turned fixed macro over its master's unrotated width and height from the macro's lower-left corner, not over its real box (OpenROAD#11735). Cells that global placement put in the part of that footprint outside the macro are then moved out by legalization, some far. `placement.rotated-macro-footprint` (PDK, platform or run [placement](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-the-process-dependent-steps) key) chooses the workaround; it acts only when a `floorplan.macros` or macros-file entry turns a macro, or when the run has a [hook](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks) before global placement (`session`, `after-floorplan`, `macro-placement`, `after-pdn` or `after-pins`), which may turn one:

| Value | Effect |
| --- | --- |
| `block` (default) | A hard placement blockage over each turned macro's unrotated footprint, grown by its [cell halo](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-the-process-dependent-steps), then outward to whole rows and sites as the legalizer rounds it, and clipped to the core, exists during global placement only, like a [`lifetime: global-placement`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls) blockage. No cell is placed where legalization would refuse it, and the area outside the macro stays empty. |
| `diamond-legalizer` | Every `detailed_placement`, including the one after post-global-route hold repair, runs `-use_diamond_legalizer`, which sees the macro's real box. |
| `none` | Neither. |

The log names each blocked footprint under `>>> Rotated macros`, or the switch to the diamond legalizer.
