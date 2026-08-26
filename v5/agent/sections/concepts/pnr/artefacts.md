## Artefacts

Per-run outputs land under `pnr/<run>/artefacts/`:

| File | Contents |
|---|---|
| `pnr.log` | Full OpenROAD log |
| `pnr.tcl` | Templated Tcl handed to OpenROAD |
| `<design>.def` | Routed DEF |
| `<design>.routed.v` | Post-route gate-level netlist |
| `<design>.routed.sdc` | Post-route SDC |
| `timing.rpt` | Worst-path timing report (full clock expanded) |
| `route.drc.rpt` | DRC violations (empty file = clean) |
| `route.maze.log` | Detail-route maze log |
| `<design>.gds` | Routed GDS — only when `--gds`/`--png` is set |
| `<design>.png` | Layout render — only when `--png` is set |
| `klayout.def2stream.log` | KLayout output for the DEF→GDS step (when used) |
| `klayout.gds2png.log` | KLayout output for the GDS→PNG render (when used) |
