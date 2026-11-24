## Flow reference

For maintainers and hook authors. The generated `pnr.tcl` runs:

1. `platform-tcl`, Liberty, LEF, a mode and scene per corner on a multi-corner platform, netlist, SDC, `max-fanout`, `layer-rc-tcl`, don't-use cells, `session` hooks. The SDC and `max-fanout` go into every mode.
2. `dont-touch` instances marked, floorplan, tracks, blockages and cut-outs, tie cells, floorplan setup repair.
3. Macro placement, tap cells, power grid, the `dont-touch` instances' supplies connected, pin placement, port buffering.
4. Global placement (with `lifetime: global-placement` and rotated-macro blockages present, and `hold-outputs-at-pins` buffers fixed), freeing those buffers, `repair_tie_fanout`, `repair_design`, legalization, `improve_placement`, `optimize_mirroring`, `placement.macro-pin-limits`, the place stage's `stage-end` hooks, `check_placement`. An illegal placement, including one a hook left, fails the stage, so no checkpoint is written and CTS does not start.
5. `repair_clock_inverters`, the `cts-root-taps` pins taken off their nets, CTS, `repair_clock_nets`, the pins put back, post-CTS setup repair, hold repair, legalization.
6. Global route, global-route repair, antenna repair, detailed route, antenna repair with detailed routes, the optional post-route repair stage, whose rounds each repair antennas after their detailed route.
7. Fill, global connections, extraction with `rcx-rules`, final reports with the `clock-latency` groups, outputs.
8. After OpenROAD: the routed-floorplan check, then stream-out and abstract.

- `stage-end` hooks run at the end of each stage, after its last global connection; the checkpoint is written after them.
- A `probe` skips the setup and hold repairs and the antenna repair after global route, and stops after global route.
- Each session must print its end marker (`>>> DONE`, `>>> End of stage <NN>_<stage>`, `>>> STOPPED after <stage>`, or the post-route round markers), or the run fails `incomplete`. This check comes before any check of the design.

A staged session starts with a prelude from the same template: the header, Liberty and LEF, `read_db` and `read_sdc` of the previous checkpoint (every mode), its saved flow variables, then step 1's per-session settings. It keeps only what the database, the checkpoint SDC and the state file hold. A resume compares each earlier stage's script up to its end-of-stage line, ignoring comments, run-directory lines, `set_thread_count` and header variables the stage does not read.

Each stage's wall time, CPU time and peak memory are recorded as `stage_end` events in `progress.jsonl` and under `resources` in the manifest; in a one-session run a stage's peak includes what earlier stages left resident. Its [timing](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#compare-timing-across-stages) goes to `stage_timing` events and `timing`.
