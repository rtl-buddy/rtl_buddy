## Keep stage checkpoints

A routing run killed at a scheduler wall limit leaves only its log, because the DEF and ODB are written after detailed routing. Set `checkpoints:` on the run to keep a database at each stage boundary:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    checkpoints: true            # or a stage, or a list: [cts, global_route]
```

| Stage | Written | Holds |
| --- | --- | --- |
| `floorplan` | before `global_placement` | floorplan, pins, tie cells, placed macros, PDN; no global-placement-only blockage |
| `place` | before `clock_tree_synthesis` | legalized global placement |
| `cts` | before `global_route` | clock tree, hold repair, legalization |
| `global_route` | after `global_route`, the `after-global-route` hooks and the [global-route repair](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-on-the-global-route), before the `before-detailed-route` hooks | the global route |
| `detailed_route` | after `detailed_route` | the detailed route, before fill |
| `post_route_repair` | after the last [post-route repair](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-on-the-detailed-route) round; only with `post-route-repair` | the repaired route, before fill |

In a one-session run the flow ends each stage, but `global_route`, on the command the table names: on entering `global_placement`, `clock_tree_synthesis` or `global_route`, or on leaving `detailed_route`. A hook that wraps one of these commands with `rename` hides that boundary, which a one-session run does not support: a run that reaches its end without writing a checkpoint it keeps fails with `fail_stage: checkpoint`, naming the stage and the command. A [staged run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions) ends each stage at the end of its session and has no such limit.

`true` keeps every stage the run has, five or, with `post-route-repair`, six; a name or list keeps some; `[]` keeps only the progress file. Unset or `false` disables the feature. Each stage writes `<NN>_<stage>.odb`, `.def` and `.sdc` into `artefacts/<run>/checkpoints/<timestamp>-<pid>/`, and `checkpoints/latest` points at the newest run. The `global_route` checkpoint adds the route guides (`.guide`) and global-route segments (`.segments`). The `detailed_route` and `post_route_repair` checkpoints keep a copy of each report the detailed router wrote, `<NN>_<stage>.route.drc.rpt` and `.route.maze.log`. The SDC is written with `write_sdc -no_timestamp -digits 9`.

- `tail -f artefacts/<run>/checkpoints/latest/progress.jsonl` follows a running flow. After a kill, the last `step_begin` with no `step_end` is the step the run was in.
- With [`hooks:`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks) set, `hook_begin` and `hook_end` events bracket each hook file and name its point and path. `hook_end` adds the status, the elapsed seconds and, on failure, the error. A `hook_begin` with no `hook_end` is the hook the run was in; rb's retained-checkpoints message names it as the step `hook <point>`.
- Each stage's wall time, CPU time and peak memory are recorded; see [Stage resources](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stage-resources).
- A checkpoint is never final. `rb power` and a plain `rb pnr-export` never read it.
- Old run directories are not pruned. Delete the ones you no longer need.

To look at a checkpoint, open its ODB in OpenROAD or stream it out with [`rb pnr-export --checkpoint`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#export-a-saved-result). To continue from one, the run must be staged; see [Resume a staged run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#resume-a-staged-run).
