---
name: rtl-buddy-implementation
description: Run and interpret rtl_buddy synthesis, P&R, power, FPGA, timing-closure, and XPLR design-space exploration workflows.
---

# rtl_buddy implementation flows

Report `rb --version` at the top of every run summary.

Use `rb --machine`; gate external tools with
`rb --machine tool-check --required-for <flow>`. Use `rb docs list` to select the
installed-version page for `synthesis`, `pnr`, `power`, `fpga`, or `xplr`.

## Results before logs

- Parse the machine payload first; use named artefact paths and logs for detail.
- Keep named-run YAML and regression manifests distinct. Confirm flags with
  `rb <flow> --help` rather than copying them between flows.
- A tool completing does not mean the design met its target. Report timing,
  area, power, routing, and guardrail fields separately from command execution.
- `synth`, `pnr`, `power`, `fpga`, `synth-regression`, `power-regression`, and
  `fpga-regression` exit 0 when every result counts as successful, 1 for any
  `FAIL` or strict `XPASS`, and 2 for a fatal configuration or environment error.
  `SKIP`, `XFAIL`, and non-strict `XPASS` count as successful. XPLR verbs exit 0
  on success and 2 on fatal errors.

## P&R traps

- **OpenROAD threads.** OpenROAD runs single-threaded unless a `pnr.yaml`,
  `power.yaml` or `synth.yaml` entry sets `threads:` (a count, or `auto` for the
  allocation). A count above the Slurm or affinity allocation is clamped with
  `openroad.threads_capped`. Quote `openroad_threads.effective`, not the
  configured value.
- **Checkpoints.** For a long or failing P&R, set `checkpoints: true` in
  `pnr.yaml`. A `FAIL` row then carries `checkpoint_dir`, `checkpoint_stages`
  and `last_step`, and `checkpoints/latest/progress.jsonl` shows the step a
  running or killed flow is in. A checkpoint is never a routed or final result:
  report it as the stage it names. `pnr-export --checkpoint <stage>` labels its
  layout `checkpoint_final: false`.
- **`pnr-export`.** It runs the KLayout export over a saved P&R result and
  starts no P&R or synthesis. The export is the whole job, so any export not
  delivered is a `FAIL`, unlike `rb pnr`, where a failed `preview` export leaves
  the P&R verdict standing. A layout published with cells that have no GDS is a
  qualified pass in `preview` and a `FAIL` in `strict`. Read `gds_status` and
  `gds_missing_cells` before reporting a layout, and `export_provenance` for
  what was read.
- **Block boundary planning.** Set `pin-constraints` (a Tcl path relative to
  `pnr.yaml`) to run pin-region commands just before `place_pins`. Do not put
  them in SDC, which is read before the die exists. `floorplan.macro-anchor`
  keeps macros off a pin edge (the packer starts in that corner).
  `floorplan.blockages` adds hard, soft or partial placement blockages, and
  macros avoid the hard ones. `floorplan.macro-placement: rtl-mp` uses
  OpenROAD's RTL-MP instead of the packer and excludes `macro-anchor`.

## Hierarchical P&R

- A `harden: true` run publishes a block's abstract. A top names it under
  `blocks:` in `pnr.yaml` and in its `synth.yaml` entry.
- `rb pnr` with no run name runs blocks before the runs that consume them.
  `--synth` also runs each upstream synthesis in that order, so a clean tree
  builds in one command. `-j N` hardens independent blocks side by side.
- A run not attempted because a block failed is `FAIL` with
  `fail_stage: blocked` and `blocked_by`. Report the block, not the top.
- A stale abstract fails naming the block and what changed. Re-harden the block
  rather than reaching for `--accept-stale`.
- A block reports 0 W in `rb power` and is listed in `unpowered_cells`, so the
  total leaves out the blocks' own power.

## Synthesis correctness gates

`rb synth` (both backends) gates three silent-corruption shapes before
reporting PPA.

- **Static-lifetime subroutines.** A `function` or `task` without an explicit
  `automatic` lifetime shares one storage location per formal across call
  sites. The gate names each `file:line: function <name>`. Fix the RTL by adding
  `automatic`; do not use `static-functions: allow`. The gate defaults to
  `error` under `frontend: slang`, so a run that passed before can fail. `warn`
  stages the migration, and `static_function_findings` in a passing result means
  the gate ran in `warn` mode and the netlist may still be wrong.
- **Conflicting drivers.** Yosys `multiple conflicting drivers` warnings fail
  the run under `conflicting-drivers: error` (a tristate bus is exempt). They
  mean a net folded to `x` and may have taken registers with it. Never report
  area or gate count from such a run.
- **Unresolved interfaces.** A `synth.unresolved_interface` warning means
  `read_verilog` could not bind an interface instance to a child's interface
  port. The instance's own port connections are dropped, so an interface
  carrying `clk` or `rst_n` leaves them undriven and the subtree loses its
  clock. Check the netlist before quoting PPA, and prefer `frontend: slang`.
  `unresolved-interfaces: error` fails the run; the default is `warn` because
  the fallback is correct when the interface has no ports the subtree reads.

A failed gate deletes the netlist, so `rb pnr` and `rb power` cannot read it.

A `synth.filelist_defines_overridden` warning means the `synth.yaml` entry's
`defines:` set a macro that the model filelist also defines with a different
value. Synthesis uses the `synth.yaml` value and simulation uses the
filelist's. Drop one of the two if the flows should agree.

## FPGA timing closure

`timing_met: false` is a completed result, not necessarily a tool crash. Start
with the worst failing path and `wns_ns`, form one hypothesis, make one focused
RTL or constraint change, rerun, and compare the same metrics. Do not paper over
a CDC or quasi-static path by relaxing the clock.

Use `rb --machine docs show concepts/fpga` for the closure decision tree and
`concepts/power` for completeness and coverage semantics.

## XPLR

XPLR records experiments and maintains the Pareto frontier; it does not propose
the next experiment. Record the hypothesis, rationale, parent, source revision,
knobs, and metric directions, then attach the observed outcome. Use
`rb --machine docs show concepts/xplr` for the manifest contract.
