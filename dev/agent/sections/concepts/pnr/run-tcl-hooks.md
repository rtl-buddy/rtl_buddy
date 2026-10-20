## Run Tcl hooks

A block that needs a step the generated flow does not have, such as its own macro placement, a density screen, an extra repair or a report, names Tcl files under `hooks:` on its run, and `rb pnr` sources them at fixed points of the flow. No edit of the generated `pnr.tcl` is needed. Each point takes one path or a list, relative to `pnr.yaml`:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    hooks:
      session: [wire_rc.tcl]
      macro-placement: [macros.tcl]
      after-cts: [io_latency.tcl, clock_report.tcl]
```

| Point | Sourced |
| --- | --- |
| `session` | After `read_sdc`, `max-fanout`, `layer-rc-tcl` and the don't-use cells, before the floorplan |
| `after-floorplan` | After the floorplan, tracks, blockages, cut-outs and tie cells, before macro placement |
| `macro-placement` | In place of rb's macro placement (the packer or RTL-MP), inside the macro branch |
| `after-pdn` | After the tap cells and the power grid, before IO pin placement |
| `after-pins` | Right after `place_pins`, before `buffer_ports` |
| `after-global-placement` | Right after `global_placement`, before `repair_tie_fanout` |
| `after-repair-design` | Right after `repair_design`, before the legalizing `detailed_placement` |
| `after-cts` | Right after `clock_tree_synthesis`, before the post-CTS legalization and the setup and hold repair, so the repair sees what the hook sets |
| `after-global-route` | Right after `global_route`, before the [global-route repair](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-on-the-global-route) |
| `before-detailed-route` | Immediately before `detailed_route`, and again before each [post-route repair round's](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-on-the-detailed-route) detailed route |
| `before-reports` | After fill insertion and parasitic extraction, before the final reports |

- The files of a point are sourced in list order at Tcl global scope, so the flow's variables are visible to them: `$OUT_DIR` and `$DESIGN` throughout, `$block` and `$macros` from macro placement on. A variable one file sets is still set in the next. `pnr.log` announces each file as `>>> Hook <point>: <path>`.
- OpenROAD runs in the run's artefact directory, `artefacts/<run>/`, so a hook reaches files next to itself through `[file dirname [info script]]`.
- `session` is for state the saved database does not keep, such as `set_wire_rc` on a run's own layers or extra `set_dont_use` cells. The PDK's `layer-rc-tcl` stays the wire RC rb knows about: the [no-wire-RC qualifier](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#set-wire-rc) and a [`netlist-source: pnr` power run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/#extracted-parasitics) read only that. The post-route don't-use check covers only the configured cells, not ones a hook adds.
- `macro-placement` runs inside the flow's macro branch, so it does not run when the netlist has no macros. The [`floorplan.macros`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-individual-macros) directives still apply before it, and the cell-halo blockages around every macro are still created after it. `$rb_movable_macros` lists the macros the directives leave unlocated, or every macro when there are no directives. Setting `floorplan.macro-placement` or `floorplan.macro-anchor` together with the hook is a config error, since neither would have an effect.
- Results come from the final reports: a report a hook prints earlier does not change the result's area, slack or TNS.
- `after-pdn` runs whether or not the run has a pdn-config.
- `pin-constraints` is unchanged: it is sourced just before `place_pins`, and `after-pins` runs after it.
- A hook that raises a Tcl error fails the run, and the error names the point and file: `Hook after-cts (<path>): <message>`. OpenROAD stops there. An OpenROAD `[ERROR ...]` line a hook catches still fails the run, as anywhere in the flow. `exit`, a top-level `break` or `continue`, and a `return` that reaches past the hook file are such errors too, since each would end the flow unfinished.
- A configured file missing from disk fails the run at `setup`, naming `hooks.<point>[<index>]`, before OpenROAD starts.
- Every hook file is an input to [stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) and to a [hardened block's](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block) abstract, so editing one makes the abstract stale. `hooks` is part of the block's configuration only when set.
- Commands a hook runs report their progress like the flow's own, but they do not move a stage checkpoint: a `global_placement` inside a hook leaves the `floorplan` checkpoint to the flow's own `global_placement`.
- In a [staged run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions), and in a one-session run with [`post-route-repair`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-on-the-detailed-route), each hook point is sourced in the session of the stage that contains it, and `session` hooks are sourced again in every session. Session state a hook sets, such as a Tcl procedure, a `rename` wrapper or `set_wire_rc`, is gone in the next session; see [what a session keeps](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions). A `session` hook keeps it in every session, but from the first one on, so it also changes the steps before the hook point that it was written for.
- `after-pins` runs before rb's `buffer_ports`. A hook that buffers the ports itself needs `buffer-ports: false`, or rb buffers them again after it.
