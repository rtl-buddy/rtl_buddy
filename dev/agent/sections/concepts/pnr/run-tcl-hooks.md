## Run Tcl hooks

`hooks:` on a run sources your Tcl at fixed points of the flow, for steps rb does not have. Each point takes a path or list, relative to `pnr.yaml`:

```yaml
    hooks:
      session: [wire_rc.tcl]
      after-cts: [io_latency.tcl]
```

| Point | Sourced |
| --- | --- |
| `session` | At the start of every session, after the SDC, before the floorplan. Also in the run's [`netlist-source: pnr` power run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/#extracted-parasitics). |
| `after-floorplan` | After floorplan, tracks, blockages, tie cells and the floorplan setup repair. |
| `macro-placement` | Instead of RTL-MP, only when the netlist has macros. `$rb_movable_macros` lists the unlocated ones. |
| `after-pdn` | After taps and the power grid. |
| `after-pins` | After pin placement, before port buffering. A hook that buffers ports needs `buffer-ports: false`. |
| `after-global-placement` | After `global_placement`. |
| `after-repair-design` | After `repair_design`, before legalization and `placement.macro-pin-limits`. |
| `before-cts` | After legalization, `improve_placement`, `optimize_mirroring` and `placement.macro-pin-limits`, before `repair_clock_inverters` and CTS. `cts-dont-use-cells` are not yet excluded. |
| `before-repair-clock-nets` | Right after `clock_tree_synthesis`, before `cts-repair-clock-nets`. `cts-dont-use-cells` are still excluded. |
| `after-cts` | After CTS and any `repair_clock_nets`, before setup and hold repair. |
| `after-timing-repair` | Right after the `repair_timing` calls of the post-CTS repair, the global-route repair and each post-route round, before their legalization. See below. |
| `after-cts-repair` | After post-CTS repair and its legalization. |
| `after-global-route` | After `global_route`, before global-route repair. |
| `after-global-route-repair` | After global-route repair, before antenna repair. It is in the `global_route` stage, so a probe runs it. |
| `before-detailed-route` | Before the flow's detailed route and each post-route round's, not the antenna repair's. |
| `after-extraction` | After `write_spef` in every session that writes one (each post-route round and the final extraction), before rb reads it. `$rb_spef` is its path. Needs `rcx-rules`. |
| `before-reports` | After fill and extraction, before the final reports. |
| `after-reports` | After the final reports, on their parasitics, before rb writes the routed outputs. |
| `stage-end` | At the end of every stage, before its checkpoint, with `$rb_stage` and `$rb_mode` (`closure` or `probe`). In post-route repair, at the end of each round, the electrical round included; when round 0 stops the rounds, none runs and the stage keeps the detailed route. |

`after-timing-repair` runs on the parasitics the repair used:

- `$rb_repair` is `post_cts`, `global_route` or `post_route`, and `$rb_repair_round` the post-route round's number.
- `$rb_setup_margin` and `$rb_hold_margin` are the margins passed to `repair_timing`, in the Liberty time unit, `0` for none. The hold repair also gets the setup margin.
- In an `eco` round it runs inside the incremental global route, before `repair-design.after-timing`'s `repair_design`. The electrical round runs it when the block sets a margin.
- It does not run after the floorplan setup repair, or where no `repair_timing` runs: a probe, or a repair block without a margin.

How hooks run:

- At Tcl global scope in `artefacts/<run>/`, with `$OUT_DIR` and `$DESIGN` set.
- A Tcl error, `exit` or stray `return` fails the run, naming the point and file, and for `stage-end` the stage. In a post-route round, where other failures only set the round aside, a `stage-end` or `after-timing-repair` error still fails the run.
- Results come from the final reports; a report a hook prints does not change them.
- On a multi-corner platform an SDC command in a hook constrains only the [primary corner's mode](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners).
- Every later `read_spef` reads what `after-extraction` hooks left: the next round's, the final reports' and `rb power`'s. A multi-corner platform reads one SPEF per corner, so one edit covers all.
- Each point belongs to the stage it runs in (`before-cts` to `cts`): a staged run runs it in that stage's session, and `--resume` from that stage runs it again. `after-timing-repair` belongs to `cts`, so a `--resume` from a later stage is refused when its files changed.
- When a run splits into sessions (`stages: true`, `--resume`, `--dispatch slurm`, `post-route-repair`), procedures, `rename` wrappers, `set_wire_rc` and `set_dont_use` a hook set are gone in the next session. Put them in a `session` hook. rb warns `pnr.hook_commands_not_kept` when a hook redefines an existing command.

rb replaces same-named files a hook wrote in `$OUT_DIR`: `timing.rpt` and `electrical.rpt` in the final reports; after the `after-reports` hooks, `<top>.def`, `<top>.routed.v`, `<top>.routed.pwr.v`, `<top>.routed.sdc`, `<top>.<corner>.routed.sdc`, `<top>.routed.odb` and, with `harden: true`, `abstract/`; and after OpenROAD exits, `worst_paths.rpt` and `fanout.rpt`. Other names are in [Inspect artefacts](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#inspect-artefacts). Every run first deletes files ending in `.def`, `.routed.v`, `.routed.pwr.v`, `.routed.sdc`, `.routed.odb`, `.routed.spef`, `.gds` or `.png`, so name a hook's files otherwise.

Hook files are inputs of checkpoints and hardened abstracts. rb does not follow `source`, so list files the SDC or a hook sources under `inputs:`, relative to `pnr.yaml`:

```yaml
    inputs: [scripts/window.tcl]
```

Each is fingerprinted by path and content with the hooks: editing one, or listing another in its place, refuses a `--resume` and makes a hardened abstract stale. `rb pnr` fails before any run starts when a run it would run lists a missing file.
