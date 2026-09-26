## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing; zero DRC lines indicate a clean route.

On a multi-corner platform, `wns_setup_ps`, `wns_hold_ps` and `tns_ps` are the worst across all corners, so summaries, gates and `xfail` markers read them unchanged. The result also carries:

- `worst_setup_corner` and `worst_hold_corner`: the corner that set each worst value. The summary shows them in a `Worst Corner` column.
- `corners`: each corner's own `wns_setup_ps`, `wns_hold_ps` and `tns_ps`, in config order.

These per-corner values are also in `pnr.log`, after `>>> Per-corner timing`. `timing.rpt` shows the worst path across all corners.

A run passes when OpenROAD exits 0 and emits no `[ERROR ...]` line, and — with `gds-mode: strict` — when the requested export was delivered complete. It skips when filtered by `reglvl` or when `tool:` is unsupported. Timing violations or DRC counts are reported as metrics; inspect the result policy for your project before using them as signoff gates.
