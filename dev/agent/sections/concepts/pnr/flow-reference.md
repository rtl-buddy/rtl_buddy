## Flow reference

For maintainers and hook authors. The generated `pnr.tcl` runs:

1. `platform-tcl`, Liberty, LEF, a mode and scene per corner on a multi-corner platform, netlist, SDC, `max-fanout`, `layer-rc-tcl`, don't-use cells, `session` hooks. The SDC and `max-fanout` go into every mode.
2. Floorplan, tracks, blockages and cut-outs, tie cells.
3. Macro placement, tap cells, power grid, pin placement, port buffering.
4. Global placement (with `lifetime: global-placement` and rotated-macro blockages present), `repair_tie_fanout`, `repair_design`, legalization, the `placement.macro-pin-limits` step.
5. CTS, optional post-CTS setup repair, hold repair, legalization.
6. Global route, optional global-route repair, detailed route, optional post-route repair stage.
7. Fill, global connections, extraction with `rcx-rules`, final reports, outputs.
8. After OpenROAD: the routed-floorplan check, then stream-out and abstract.

The `stage-end` hooks run at the end of each stage, after its last global connection; the stage's checkpoint is written after them.

A `probe` skips the setup and hold repair and stops after global route. Each session must print its end marker (`>>> DONE`, `>>> End of stage <NN>_<stage>`, `>>> STOPPED after <stage>`, or the post-route round markers), or the run fails `incomplete`. This check comes before any check of the design.

A staged session starts with a prelude regenerated from the same template: the header, Liberty and LEF, `read_db` and `read_sdc` of the previous checkpoint (every mode), its saved flow variables, then step 1's per-session settings. It keeps only what the database, the checkpoint SDC and the state file hold. A resume compares each earlier stage's script up to its end-of-stage line, ignoring comments, the run-directory lines, `set_thread_count` and header variables the stage does not read.

Each stage's wall time, CPU time and peak memory are recorded as `stage_end` events in `progress.jsonl` and under `resources` in the manifest. In a one-session run the peak memory of a stage includes what earlier stages left resident. Its [timing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#compare-timing-across-stages) is recorded as `stage_timing` events and under `timing`.
