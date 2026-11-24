## Fix a failed run

Read the row's description and `fail_stage`, then `pnr.log`, and find the stage or message below. A result with any `fail_stage` failed before or instead of a verdict on the design, so `xfail:` never excuses it. A failure without one (an OpenROAD error, a don't-use violation, `fail-on-electrical`, a [signoff gate](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#gate-signoff)) can be excused.

| `fail_stage` | Cause and fix |
| --- | --- |
| `setup` | A configuration or input problem found before OpenROAD started, named in the description: a missing file (`pdn-config`, `rcx-rules`, a hook, a planner file), a bad planner-file entry, `post-route-repair` without `rcx-rules`, blockages on OpenROAD older than 26Q1, `cts-obstruction-aware: false` on an OpenROAD without `-no_obstruction_aware`, a per-corner `constraints:` that does not fit the platform, or no answer to the [SDC mode check](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners). |
| `incomplete` (`the flow did not finish`) | A sourced Tcl file (PDK Tcl, `pdn-config`, `pin-constraints`, a hook) called `exit` or `return -level 2`, ending OpenROAD with code 0. Remove the exit from the file the named last step sources. |
| `routed_check` | A located macro or exact pin moved after the floorplan, usually by a `pin-constraints` file or hook. |
| `pins_complete` | `pins-complete: true` found a port with no pin entry or conflicting entries. |
| `checkpoint` | A checkpoint could not be written (disk full; see the `checkpoint` event in `progress.jsonl`), or in a one-session run a hook wrapped `global_placement`, `clock_tree_synthesis`, `global_route` or `detailed_route` with `rename`. Free space, remove the wrapper, or set `stages: true`. |
| `resume` | A stage script, input file, checkpoint or OpenROAD version differs. The description lists each script and input difference with its [`--accept-change`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#accept-a-change-on-resume) value; otherwise rerun from an earlier stage. |
| `dispatch` | A Slurm stage job ended `OUT_OF_MEMORY`, `TIMEOUT`, `CANCELLED` or similar, never started, or was refused. Read `dispatch/<NN>_<stage>.slurm.log` and raise that stage's [`resources:`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#dispatch-the-stages-to-slurm). |
| `abstract` | A hardened block's view could not be written. Read `pnr.log`. |
| `outputs` | rb could not read, decode or rewrite `<top>.routed.pwr.v` or a hardened `<top>.lib` to put supply ports last; the description names the file and error. |
| `export` | Stream-out failed (KLayout, `klayout-tech`, an input file, the render) under `gds-mode: strict` in `rb pnr`, or in any mode in `rb pnr-export`. Fix it and run `rb pnr-export`. |
| `blocked`, `synth`, `error` | A block this run uses failed, its synthesis failed under `--synth`, or rb crashed (exception in the description). |

| Message | Cause and fix |
| --- | --- |
| `[ERROR PDN-0179] Unable to repair all channels` | Macros too close for the power grid. Set `placement.macro-channel`. |
| `macro ... overlaps floorplan.blockages[N]` (or `floorplan.core-cutouts[N]`) | A placed macro sits on a `hard` blockage or core cut-out. Locate it with `floorplan.macros`, enlarge `placement.macro-channel`, or move the blockage. |
| `PDN-0008 ... halo overlaps row` | A macro grid's `-halo` reaches a row the macro does not sit on. [Cut the rows](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#plan-the-power-grid) that far. |
| `PDN-0233` on a turned macro or block grid | The grid has no via for that macro. See [Place macros](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-macros) and [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block). |
| `GRT-0116`, global-route overflow | Too congested. `failed_global_route` names the checkpoint with the failed route and a copy of `congestion.rpt`; open it with `rb pnr-export --checkpoint` or `read_db`. Lower utilization or density, try `placement.routability-driven`, or [tune global routing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-global-routing). Congestion between macros or at the core edge means RTL-MP left no channel: set `placement.macro-channel`, such as ORFS's `[80, 80]` on sky130hd. |
| Large `drc_count` | Try `routing-layer-adjustment`, a lower density, or `post-route-repair` with several rounds. |
| `RSZ-0090` at a slow corner | A macro with one Liberty limits that corner. [Map `lib-paths` per corner](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners). |
| Macro pins in `electrical.rpt`, `pnr.macro_pin_limits_unmet` | Set `placement.macro-pin-limits`, add a stronger buffer to it, or lower `macro-cell-halo`. See [Tune placement](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-placement-clock-tree-and-repair). |
| Slow output pins at signoff, yet no max-slew violators | Ports have no limit unless the SDC sets one. See [Check and repair slew at output ports](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#check-and-repair-slew-at-output-ports). |
| `RSZ-0074` in a post-route repair round | Set `post-route-repair.reroute: full`. |
| `RSZ-0089 Could not find a resistance value for any corner` | Setup repair ran on zero wire RC. [Set `layer-rc-tcl`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#set-wire-rc). |
| `CTS-0080 Sink not found` | Set `cts-sink-clustering: false`; with macros, `cts-obstruction-aware: false` also avoids it. |
| `GRT-0273 Disabled NDR`, large hold violations | Clock nets with non-default rules detour. Set `cts-apply-ndr: none`. |
| `DPL-0011` | A macro off the row grid (`macro-snap: false`). Put its bottom edge on a row. |
| `IFP-0039` at `make_tracks` (ASAP7) | Set the PDK's `tracks-tcl`. |
| `RB-DONT-USE-VIOLATION` | The routed design uses an excluded cell, usually from the netlist. Change the RTL, synthesis or pattern. |
| `STA-0122 cell '<pattern>' not found` | A `dont-use-cells` or `cts-dont-use-cells` pattern matches nothing. |
| `openroad.threads_capped` | `threads:` exceeds the allocation. Raise the reservation or use `threads: auto`. |
| `floorplan.pins[i]: no port matches`, `floorplan.macros[i]: no instance matches` | Fix the name or glob. A macro name is the netlist's, brackets included; see [Place macros](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-macros). |
| `floorplan.macros: the netlist has no hard macros` | Check that synthesis kept the macro instances. |
| `floorplan: no rows left for N standard cell(s)` | The blocks cover the core. Leave a strip of core without blocks, or remove the cells, such as port buffers with `buffer-ports: false`. See [Assemble hardened blocks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#assemble-hardened-blocks). |
| Missing or stale block abstract, platform or corner mismatch | Run the `rb pnr` command the message names, or `rb pnr` with no run name. |
| `block power: ...` | A block's top-layer supply pins miss the parent's straps. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block). |
| `port 'd' is connected to N bit(s)`, `instance ... sets <P>=...` | The parent's ports or parameters differ from the hardened block's. See [Instance a parameterised block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#instance-a-parameterised-block). |
| Run refused: earlier dispatched jobs still queued | Wait, or run the printed `scancel`. If `squeue` cannot answer, remove the named `dispatch/` directory once the jobs have ended. |
