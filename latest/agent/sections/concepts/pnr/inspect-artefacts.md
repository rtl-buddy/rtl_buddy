## Inspect artefacts

Outputs land under `<pnr-dir>/artefacts/<run>/`.

| File | Purpose |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output and generated flow |
| `<top>.def`, `<top>.routed.v`, `<top>.routed.sdc` | Routed DEF, post-route netlist and constraints |
| `<top>.routed.odb` | OpenROAD database read by post-P&R `rb power` |
| `<top>.routed.spef` | Extracted parasitics; only when the PDK sets `rcx-rules` |
| `timing.rpt` | Worst-path timing across all corners |
| `route.drc.rpt`, `route.maze.log` | DRC summary and detailed-route log |
| `<top>.gds`, `<top>.png`, `klayout.*.log` | Optional KLayout outputs and logs |
| `export.provenance.json` | What the last `rb pnr-export` read and produced |
| `checkpoints/`, `abstract/` | Optional [stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#keep-stage-checkpoints) and [hardened-block abstract](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#harden-a-block) |

Each run deletes the previous run's outputs first, and a run that fails after writing the routed database removes it again, so `rb power` never reads a stale one. `pnr.log` and `pnr.tcl` are kept from a failed run.

On failure, read `pnr.log`. If only KLayout failed, read the matching `klayout.*.log`, fix the installation and rerun with `--gds` or `--png`.
