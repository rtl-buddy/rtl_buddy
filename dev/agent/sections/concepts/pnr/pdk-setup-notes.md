## PDK setup notes

- Nangate45 has no `pdn-config`, so its runs have no power grid. Set `pin-layers`, for example `metal3` and `metal2`. For extraction, use ORFS's `flow/platforms/nangate45/rcx_patterns.rules`.
- The template's `sky130hd` entry is a worked example: `pin-layers` `met3` and `met2`, `met*` routing layers, a `pdn-config`, `placement.density: 0.60`, a `dont-use-cells` list for the probe and `lpflow` cells, a `cts-buffer` list, and ORFS's `sky130hs/rcx_patterns.rules`. On sky130hd, set `port-buffer` for hardened blocks.
- For ASAP7:
  - Set the four Tcl keys to ORFS's `liberty_suppressions.tcl`, `setRC.tcl`, `openRoad/make_tracks.tcl`, and a copy of `openRoad/tapcell.tcl` with `TAP_CELL_NAME` and `MACRO_ROWS_HALO_X` / `_Y` filled in.
  - List each corner's split Liberty files under that corner.
  - List each threshold-voltage flavour's cell LEF (`asap7sc7p5t_28_R_1x_*.lef`, `..._L_...`) in `macro-lef`, a list read in order, and macro LEFs in `lef-paths`. In a Tcl hook, `$MACRO_LEF` names only the first.
  - Give `fill-cells` each flavour's fillers, or fill insertion fails with `DPL-0050`.
  - ORFS uses `routing-layer-adjustment: 0.25`.
