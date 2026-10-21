## Fix a failed run

Read the row's description and `fail_stage` first, then `pnr.log`. A result with any `fail_stage` failed before or instead of a verdict on the design, so `xfail:` never excuses it. A failure without `fail_stage` (an OpenROAD error, a don't-use violation, `fail-on-electrical`, a [signoff gate](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#gate-signoff)) can be excused.

| Symptom | Cause and fix |
| --- | --- |
| `fail_stage: setup` | Configuration or input problem found before OpenROAD started: a missing file (`pdn-config`, `rcx-rules`, a hook, a planner file), a bad planner-file entry, `post-route-repair` without `rcx-rules`, blockages on OpenROAD older than 26Q1, a per-corner `constraints:` that does not fit the platform, or an OpenROAD that does not answer the [SDC mode check](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners). The description names it. |
| `fail_stage: incomplete`, `the flow did not finish` | A Tcl file the flow sources (PDK Tcl, `pdn-config`, `pin-constraints`, a hook) called `exit` or `return -level 2`, ending OpenROAD with code 0. The description names the last step reached; remove the exit from the file that step sources. |
| `fail_stage: routed_check` | A located macro or exact pin moved between floorplan and the routed DEF, usually because a `pin-constraints` file or hook moved it. |
| `fail_stage: pins_complete` | `pins-complete: true` found a port with no pin entry, or with conflicting entries. Add or remove entries. |
| `fail_stage: checkpoint` | A checkpoint could not be written (disk full; see the `checkpoint` event in `progress.jsonl`), or a hook wrapped `global_placement`, `clock_tree_synthesis`, `global_route` or `detailed_route` with `rename` in a one-session run. Free space, or remove the wrapper or set `stages: true`. |
| `fail_stage: resume` | `--resume` refused: a stage script, input file, checkpoint or OpenROAD version differs. Rerun from an earlier stage. |
| `fail_stage: dispatch` | A Slurm stage job ended `OUT_OF_MEMORY`, `TIMEOUT`, `CANCELLED` or similar, never started, or was refused. Read `dispatch/<NN>_<stage>.slurm.log`; raise that stage's `resources:`. See [Dispatch](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#dispatch-the-stages-to-slurm). |
| `fail_stage: abstract` | A hardened block's view could not be written. Read `pnr.log`. |
| `fail_stage: outputs` | rb could not read, decode or rewrite `<top>.routed.pwr.v` or a hardened block's `<top>.lib` to put the supply ports last. The description names the file and error. |
| `fail_stage: export` | Stream-out failed, under `gds-mode: strict` in `rb pnr` or in any mode in `rb pnr-export`: KLayout, `klayout-tech`, an input file or the render. Fix it and run `rb pnr-export`. |
| `fail_stage: blocked`, `synth`, `error` | A block this run uses failed, its upstream synthesis failed under `--synth`, or rb crashed (the exception is in the description). Fix the cause first. |
| `[ERROR PDN-0179] Unable to repair all channels` | Macros too close for the power grid. Raise `placement.macro-halo` (about 19 µm minimum on sky130hd). |
| `PDN-0008 ... halo overlaps row` | A macro grid's `-halo` in `pdn-config` reaches into a row. Reduce it to the value the message suggests. |
| `PDN-0233` on a turned macro or a block's grid | The power grid has no via for that macro. See [Place macros](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-macros) and [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block). |
| `N macros do not fit the W x H um floorplan core` | Lower `utilization`, change `aspect`, or lower `placement.macro-halo`. The message gives the smallest core that fits. |
| `GRT-0116`, global-route overflow | Too congested. The result's `failed_global_route` names the checkpoint (`<run-id>/global_route`) that holds the failed route's database and a copy of `congestion.rpt`; open it with `rb pnr-export --checkpoint` or `read_db` in OpenROAD. Lower utilization or density, try `placement.routability-driven`, or [tune global routing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-global-routing). |
| Large `drc_count` | Try `routing-layer-adjustment`, a lower density, or `post-route-repair` with several rounds. |
| `RSZ-0090` at a slow corner | A macro with one Liberty limits that corner. Map each corner to the macro's Liberty for it in `lib-paths`; see [Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners). |
| Macro pins in `electrical.rpt`, or `pnr.macro_pin_limits_unmet` | Set `placement.macro-pin-limits`, add a stronger buffer to it, or lower the macro's `macro-cell-halo`. See [Tune placement](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-placement-clock-tree-and-repair). |
| `RSZ-0074` in a post-route repair round | Set `post-route-repair.reroute: full`. |
| `CTS-0080 Sink not found` | Set `cts-sink-clustering: false`. |
| `GRT-0273 Disabled NDR`, large hold violations | Clock nets with non-default rules detour. Set `cts-apply-ndr: none`. |
| `DPL-0011` | A macro placed off the row grid (`macro-snap: false`). Put its bottom edge on a row. |
| `IFP-0039` at `make_tracks` (ASAP7) | Set the PDK's `tracks-tcl`. |
| `RB-DONT-USE-VIOLATION` | The routed design uses an excluded cell, usually one the netlist already had. Change the RTL, synthesis or pattern. |
| `STA-0122 cell '<pattern>' not found` | A `dont-use-cells` or `cts-dont-use-cells` pattern matches nothing. |
| `openroad.threads_capped` | `threads:` exceeds the allocation. Raise the reservation or use `threads: auto`. |
| `floorplan.pins[i]: no port matches`, `floorplan.macros[i]: no instance matches` | Fix the name or glob. |
| `floorplan.macros: the netlist has no hard macros` | Located macros place nothing; check that synthesis kept the macro instances. |
| Missing or stale block abstract, platform/corner mismatch | Run the `rb pnr` command the message names, or `rb pnr` with no run name. |
| `block power: ...` | A block's top-layer supply pins miss the parent's straps. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block). |
| `port 'd' is connected to N bit(s)`, `instance ... sets <P>=...` | The parent instances a block with other ports or parameters than it was hardened with. See [Parameterised blocks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#instance-a-parameterised-block). |
| Run refused: earlier dispatched jobs still queued | Wait, or run the `scancel` it prints. If `squeue` cannot answer, remove the `dispatch/` directory it names once the jobs have ended. |
