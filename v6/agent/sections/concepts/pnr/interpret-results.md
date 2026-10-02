## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing and zero DRC lines indicate a clean route. `wns_setup_ps`, `wns_hold_ps` and `tns_ps` are converted to picoseconds from the Liberty `time_unit`; see [Synthesis: Configure tools and the PDK](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#configure-tools-and-the-pdk).

A run passes when OpenROAD exits 0 with no `[ERROR ...]` line and, under `gds-mode: strict`, the requested export was delivered complete. It skips when `reglvl` filters it out or `tool:` is unsupported. Timing violations and DRC counts are metrics only; gate signoff on them in your project.

On a multi-corner platform, `wns_setup_ps`, `wns_hold_ps` and `tns_ps` are the worst across corners. The result also names the `worst_setup_corner` and `worst_hold_corner` and lists each corner's own values; `pnr.log` has them after `>>> Per-corner timing`.
