## PDK setup notes

The same flow runs on any PDK the root config declares. Switching PDKs is a `platform:` change in the run YAMLs.

- **Nangate45 (FreePDK45).** All defaults are calibrated on it. Set `site`, one Liberty corner, `tech-lef`, `macro-lef`, the tie and fill cells and `cts-buffer: BUF_X4`, and leave every process key unset. It has no PDN snippet, so runs have no power grid. For extraction, set `rcx-rules` to ORFS' `flow/platforms/nangate45/rcx_patterns.rules`; its LEF has no via resistance, so vias extract as 0 Ω.
- **sky130hd.** The template's `sky130hd` PDK entry is a worked example, used by the `demo_tiny_alu_subsys_hier` runs. Beyond Nangate45's fields it needs `pin-layers` (`met3` / `met2`), `routing-layers` in `met*` names, a `pdn-config`, `placement.density: 0.60`, a `dont-use-cells` list for the probe and `lpflow` cells, and a `cts-buffer` list. Use ORFS' `flow/platforms/sky130hs/rcx_patterns.rules` for `rcx-rules`.
- **ASAP7.** Not validated end to end. Follow ORFS' `flow/platforms/asap7` with a much lower placement density. The flow does not run ORFS' `tapcell`, tracks file or layer-RC file. List each corner's split Liberty files under that corner. `macro-lef` takes one path, so list the extra LEFs in `lef-paths`.
