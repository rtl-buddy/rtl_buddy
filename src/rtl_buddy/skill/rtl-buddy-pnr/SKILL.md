---
name: rtl-buddy-pnr
description: Set up, run, read and debug rtl_buddy place-and-route (rb pnr, rb pnr-export) on OpenROAD, including staged runs, Slurm stage dispatch and hardened blocks.
---

# rtl_buddy place-and-route

Report `rb --version` at the top of every run summary.

Use `rb --machine`. Field definitions: `rb --machine docs show reference/yaml#pnryaml`. Behaviour and the failure table: `rb --machine docs show concepts/pnr` (sections can be fetched as `concepts/pnr#<anchor>`). Gate tools with `rb --machine tool-check --required-for pnr`.

## Set up a run

- A `pnr.yaml` run needs `name`, `desc`, `synth` + `synth-path` (that synthesis must have written its netlist), `constraints` (SDC) and `platform` (a `cfg-pnr-platforms` entry in `root_config.yaml`). Paths are relative to `pnr.yaml`.
- Process files and defaults live on the `cfg-pdks` entry (`pdn-config`, `rcx-rules`, `layer-rc-tcl`, `placement.*`, `dont-use-cells`); the platform picks `corner` or `corners` and overrides knobs; the run overrides `placement:` and the repair blocks.
- Copy an existing run in the project before writing one from scratch. Find names with `rb --machine pnr --list -c <pnr.yaml>`.
- Put extra Tcl in `hooks:` (fixed flow points) or `pin-constraints`, never in an edited `pnr.tcl` or the SDC.
- Multi-corner (`corners:`): each corner is timed in its own SDC mode (OpenROAD 26Q4+). `constraints:` may map each corner to its SDC. An SDC command in a hook constrains only the primary (first) corner; put per-corner constraints in the SDCs.

## Run loop

```bash
rb --machine pnr <run> -c <pnr.yaml>                     # one run
rb --machine pnr -c <pnr.yaml> --synth -j 4              # whole suite, blocks first, synthesis included
rb --machine pnr <run> -c <pnr.yaml> --stop-after global_route
rb --machine pnr <run> -c <pnr.yaml> --resume latest --from detailed_route
rb --machine pnr <run> -c <pnr.yaml> --dispatch slurm    # one Slurm job per stage
rb --machine pnr-export <run> -c <pnr.yaml> --png        # GDS/PNG from a saved result
```

- Explore a floorplan with `mode: probe` (no timing repair, stops after global route) before full closure runs.
- For long runs set `checkpoints: true`, or `stages: true` to make the run resumable. `--resume` reuses earlier stages only if their scripts, inputs and OpenROAD version are unchanged; it is refused otherwise (`fail_stage: resume`).
- OpenROAD is single-threaded unless the run sets `threads:` (`auto` = the allocation's CPUs). Quote `openroad_threads.effective`.
- `--dispatch slurm` sizes each stage from the run's `resources:` (`stages.<stage>` overrides). `rb pnr` blocks until the chain ends; Ctrl-C cancels its jobs.
- `pnr.log` is live. Before calling a route hung, check `DRT-0195` iterations and `DRT-0199` violation counts, and `checkpoints/latest/progress.jsonl` for the current step.

## Read results

- `pnr` and `pnr-export` exit 0 when every result counts as successful (`PASS`, `SKIP`, `XFAIL`, non-strict `XPASS`), 1 for any `FAIL` or strict `XPASS`, and 2 for a fatal configuration or environment error.
- `PASS` means the flow finished, not that the design closed. Report `wns_setup_ps`, `wns_hold_ps`, `tns_ps`, `tns_hold_ps`, `drc_count` and the three `max_*_violation_count` fields separately from the verdict.
- Compare experiments on `routed_cell_count`, not `cell_count` (the input netlist).
- `routed: false` (probe, `--stop-after`) means the numbers are a stage estimate; never report them as routed results.
- Read qualifiers in `desc`: `pnr.no_wire_rc` (timing before routing had no wire RC; set the PDK's `layer-rc-tcl`), electrical violators, a passed-over or failed post-route repair round, `GDS incomplete`, accepted stale abstracts.
- Multi-corner: WNS fields are the worst over corners; `worst_setup_corner`, `worst_hold_corner` and `corners` give the breakdown. `max_fanout_violation_count` counts a pin once per corner. `<top>.<corner>.routed.sdc` is each corner's SDC for signoff STA.
- `post_route_repair.kept_round`, `stop` and `reason` say what the repair rounds achieved.
- Stream-out: `gds_status` and `gds_missing_cells`. In `gds-mode: preview` a missing cell layout only qualifies the `PASS`; in `strict`, and in `pnr-export`, an export not delivered is a `FAIL`.

## Debug a failure

1. Read `desc` and `fail_stage`. Any `fail_stage` means the run failed before a design verdict, and `xfail:` does not excuse it.
2. Look the message or `fail_stage` up in `concepts/pnr#fix-a-failed-run`.
3. Read `artefacts/<run>/pnr.log` from the last `>>> ` step marker; for staged runs the per-stage logs are in `checkpoints/<run-id>/`.

Frequent cases:

- `fail_stage: setup`: a missing file or bad config, named in `desc`. Nothing ran.
- `fail_stage: incomplete`: a sourced Tcl file called `exit`; remove it.
- `fail_stage: dispatch`: the stage job was killed by Slurm; read `dispatch/<NN>_<stage>.slurm.log` and raise that stage's `resources:`.
- `PDN-*` errors: macro halo, macro grid or a turned macro; `N macros do not fit`: lower `utilization`.
- Global-route overflow or high `drc_count`: lower density or utilization, `placement.routability-driven`, `routing-layer-adjustment`.
- Macro pins in `electrical.rpt`, such as SRAM inputs with a tight `max_transition`: `placement.macro-pin-limits`; `macro_pin_unmet_count` counts the pins it could not fix.
- `RB-DONT-USE-VIOLATION`: the netlist uses an excluded cell; fix synthesis, not P&R.
- Change one knob per rerun and compare the same fields.

## Hierarchical designs

- A block run sets `harden: true` (single-corner platform, KLayout required) and publishes `artefacts/<run>/abstract/`. Set the platform's `port-buffer`.
- A top names blocks under `blocks:` in its `pnr.yaml` and in its `synth.yaml` entry, and its filelist keeps the module a blackbox.
- `rb pnr -c <pnr.yaml> --synth` with no run name builds blocks, then their consumers. `fail_stage: blocked` with `blocked_by` means fix the block, not the top.
- A stale abstract fails naming what changed. Re-harden the block instead of passing `--accept-stale`.
- A block's supply pins must line up with the parent's top-layer straps (`block power: ...` errors). Parameter overrides on a block instance must match its hardened `params:`.
- A parent's `rb power` counts each block as 0 W; run `rb power` on the block.
