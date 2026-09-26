## Inspect artefacts

Outputs land under `<pnr-dir>/artefacts/<run>/`.

| File | Purpose |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output and generated flow |
| `def2stream.inputs.json` | GDS, LEF and allow-empty list the optional KLayout stream-out read |
| `def2stream.report.json` | Which cells the stream-out could not fill, and whether it was complete |
| `<top>.def` | Routed DEF |
| `<top>.routed.v` | Post-route gate-level netlist |
| `<top>.routed.sdc` | Post-route constraints |
| `<top>.routed.odb` | OpenROAD database used by post-P&R power |
| `<top>.routed.spef` | OpenRCX-extracted parasitics; only when the PDK sets `rcx-rules` |
| `timing.rpt` | Expanded worst-path timing |
| `route.drc.rpt`, `route.maze.log` | DRC summary and detailed-route log |
| `<top>.gds`, `<top>.png` | Optional KLayout outputs |
| `export.provenance.json` | What an `rb pnr-export` invocation read and produced |
| `klayout.*.log` | Optional conversion logs |
| `checkpoints/` | Optional [stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints), one directory per run, plus `latest` |
| `abstract/` | Optional [hardened-block abstract](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block): LEF, Liberty, GDS and manifest |

Every file above except the logs is deleted before each run — including the optional KLayout outputs, which are cleared up front rather than at the streamout step, so a run that dies inside OpenROAD or on a host without KLayout leaves no older layout behind. A run that dies short of routing therefore leaves the outputs it never wrote absent rather than the previous run's. Unlike the other flows, this happens even when OpenROAD itself is missing — the clear is the first thing a run does — because `rb power` resolves `<top>.routed.odb` by path and must never be handed the previous run's database. For the same reason a run that reaches `write_db` and then dies — killed, exiting non-zero, or logging an `[ERROR ...]` line — has its outputs removed again, so a `FAIL` never leaves a routed database behind. `pnr.tcl` and `def2stream.inputs.json` are cleared only in that first up-front pass, so a rerun that never reaches script generation does not leave the previous run's flow script or stream-out inputs looking like the ones it used — but a run that does reach the tools keeps them even when it fails, because they are what `pnr.log` and `klayout.def2stream.log` are logs of. The optional KLayout steps behave the same: a zero-length GDS, a half-rendered PNG, and the stream-out report that would otherwise say a layout is complete are removed rather than left to be read as this run's. A `strict` export failure removes the layout and its report but keeps the routed outputs, because P&R itself succeeded. `checkpoints/` is the exception to all of this: its run directories are never cleared, and only its `latest` pointer is removed up front. `rb pnr-export` clears a narrower set still — the layout, the image, the stream-out report, the input manifest and its own record — and never the routed DEF, ODB, netlist or SDC it reads. The routed SPEF is cleared with the ODB, up front and on every failure, so a run on a PDK that no longer sets `rcx-rules` cannot leave the previous run's extraction beside its fresh database for `rb power` to read. On failure, inspect `pnr.log`. If KLayout alone failed, inspect the corresponding `klayout.*.log` and rerun with `--gds` or `--png` after correcting the installation.
