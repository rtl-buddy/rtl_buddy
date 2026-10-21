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
| `after-floorplan` | After floorplan, tracks, blockages and tie cells |
| `macro-placement` | Instead of rb's macro placement; only when the netlist has macros. `$rb_movable_macros` lists the unlocated ones. Not with `macro-placement: rtl-mp` or a non-default `macro-anchor`. |
| `after-pdn` | After taps and the power grid |
| `after-pins` | After pin placement, before port buffering. A hook that buffers ports needs `buffer-ports: false`. |
| `after-global-placement` | After `global_placement` |
| `after-repair-design` | After `repair_design`, before legalization and the `placement.macro-pin-limits` step |
| `after-cts` | After CTS, before setup and hold repair |
| `after-global-route` | After `global_route`, before global-route repair |
| `before-detailed-route` | Before each detailed route, including each post-route repair round's |
| `before-reports` | After fill and extraction, before the final reports |

Hooks run at Tcl global scope in `artefacts/<run>/`, with `$OUT_DIR` and `$DESIGN` set. A Tcl error, `exit` or stray `return` in a hook fails the run naming the point and file. Results come from the final reports, so a report a hook prints earlier does not change them. Hook files are inputs of checkpoints and hardened abstracts. On a multi-corner platform an SDC command in a hook constrains the primary corner only; see [Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners).

rb does not read the Tcl for `source` targets. List the files the SDC or a hook sources under `inputs:`, relative to `pnr.yaml`. Each is fingerprinted by path and content with the hook files, so editing one or listing another in its place refuses a `--resume` and makes a hardened block's abstract stale. `rb pnr` fails before any run starts when a run it would run lists a missing file:

```yaml
    inputs: [scripts/window.tcl]
```

When a run splits into several OpenROAD sessions (`stages: true`, `--resume`, `--dispatch slurm`, or `post-route-repair`), Tcl procedures, `rename` wrappers, `set_wire_rc` and `set_dont_use` set by a hook are gone in the next session. Put such state in a `session` hook. rb warns `pnr.hook_commands_not_kept` when a hook redefines an existing command.
