## Run Tcl hooks

`hooks:` on a run sources your Tcl files at fixed points of the flow, for steps rb does not have. Each point takes a path or a list, relative to `pnr.yaml`:

```yaml
    hooks:
      session: [wire_rc.tcl]
      after-cts: [io_latency.tcl]
```

| Point | Sourced |
| --- | --- |
| `session` | At the start of every OpenROAD session, after the SDC, before the floorplan |
| `after-floorplan` | After floorplan, tracks, blockages, tie cells and the floorplan setup repair |
| `macro-placement` | Instead of RTL-MP; only when the netlist has macros. `$rb_movable_macros` lists the unlocated ones. |
| `after-pdn` | After taps and the power grid |
| `after-pins` | After pin placement, before port buffering. A hook that buffers ports needs `buffer-ports: false`. |
| `after-global-placement` | After `global_placement` |
| `after-repair-design` | After `repair_design`, before legalization and the `placement.macro-pin-limits` step |
| `before-cts` | After legalization, `improve_placement`, `optimize_mirroring` and the `placement.macro-pin-limits` step, before `repair_clock_inverters` and `clock_tree_synthesis`. The `cts-dont-use-cells` are not yet excluded. |
| `before-repair-clock-nets` | Right after `clock_tree_synthesis`, before `cts-repair-clock-nets` runs `repair_clock_nets`. The `cts-dont-use-cells` are still excluded. |
| `after-cts` | After CTS and any `repair_clock_nets`, before setup and hold repair |
| `after-cts-repair` | After post-CTS setup and hold repair and their legalization |
| `after-global-route` | After `global_route`, before global-route repair |
| `after-global-route-repair` | After global-route repair, before the antenna repair, in the `global_route` stage, so also in a probe |
| `before-detailed-route` | Before the flow's detailed route and each post-route repair round's; not before the antenna repair's |
| `after-extraction` | In every session that writes a SPEF (each post-route repair round's extraction and the final one), after `write_spef` and before rb reads it. `$rb_spef` is its path. Needs `rcx-rules`. |
| `before-reports` | After fill and extraction, before the final reports |
| `after-reports` | After the final reports, on their parasitics, before rb writes the routed outputs |
| `stage-end` | At the end of every stage, before its checkpoint, with `$rb_stage` naming the stage and `$rb_mode` the run's `mode` (`closure` or `probe`). In post-route repair, at the end of each repair round, including the electrical round; when round 0 stops the rounds, the stage keeps the detailed route and runs none. |

Hooks run at Tcl global scope in `artefacts/<run>/`, with `$OUT_DIR` and `$DESIGN` set. A Tcl error, `exit` or stray `return` in a hook fails the run naming the point and file; a `stage-end` hook's error also names the stage, and fails the run in a post-route repair round too, where other failures only set the round aside. Results come from the final reports, so a report a hook prints before or after them does not change them. Hook files are inputs of checkpoints and hardened abstracts. On a multi-corner platform an SDC command in a hook constrains the primary corner only; see [Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners).

Each point belongs to the stage it runs in, `before-cts` to `cts`: a staged run runs it in that stage's session where a one-session run runs it, and `--resume` from that stage runs it again.

Every later `read_spef` of a SPEF reads what the `after-extraction` hooks left: the next post-route repair round's, the final reports' and `rb power`'s. A multi-corner platform writes one SPEF and reads it once per corner, so one edit covers every corner.

rb writes into `$OUT_DIR` and replaces a file of the same name a hook wrote there: `timing.rpt` and `electrical.rpt` in the final reports, then, after the `after-reports` hooks, `<top>.def`, `<top>.routed.v`, `<top>.routed.pwr.v`, `<top>.routed.sdc`, `<top>.<corner>.routed.sdc` and `<top>.routed.odb`, and with `harden: true` the `abstract/` views. The other names are in [Inspect artefacts](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#inspect-artefacts). Every run first deletes the files ending in `.def`, `.routed.v`, `.routed.pwr.v`, `.routed.sdc`, `.routed.odb`, `.routed.spef`, `.gds` or `.png`, so give a hook's own files other names.

rb does not read the Tcl for `source` targets. List the files the SDC or a hook sources under `inputs:`, relative to `pnr.yaml`. Each is fingerprinted by path and content with the hook files, so editing one or listing another in its place refuses a `--resume` and makes a hardened block's abstract stale. `rb pnr` fails before any run starts when a run it would run lists a missing file:

```yaml
    inputs: [scripts/window.tcl]
```

When a run splits into several OpenROAD sessions (`stages: true`, `--resume`, `--dispatch slurm`, or `post-route-repair`), Tcl procedures, `rename` wrappers, `set_wire_rc` and `set_dont_use` set by a hook are gone in the next session. Put such state in a `session` hook. rb warns `pnr.hook_commands_not_kept` when a hook redefines an existing command.
