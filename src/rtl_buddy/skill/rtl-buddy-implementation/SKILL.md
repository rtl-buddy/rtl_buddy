---
name: rtl-buddy-implementation
description: Run and interpret rtl_buddy synthesis, power, FPGA, timing-closure, and XPLR design-space exploration workflows.
---

# rtl_buddy implementation flows

Report `rb --version` at the top of every run summary.

Use `rb --machine`; gate external tools with
`rb --machine tool-check --required-for <flow>`. Use `rb docs list` to select the
installed-version page for `synthesis`, `power`, `fpga`, or `xplr`.

## Results before logs

- Parse the machine payload first; use named artefact paths and logs for detail.
- Keep named-run YAML and regression manifests distinct. Confirm flags with
  `rb <flow> --help` rather than copying them between flows.
- A tool completing does not mean the design met its target. Report timing,
  area, power, routing, and guardrail fields separately from command execution.
- `synth`, `power`, `fpga`, `synth-regression`, `power-regression`, and
  `fpga-regression` exit 0 when every result counts as successful, 1 for any
  `FAIL` or strict `XPASS`, and 2 for a fatal configuration or environment error.
  `SKIP`, `XFAIL`, and non-strict `XPASS` count as successful. XPLR verbs exit 0
  on success and 2 on fatal errors.

## Place-and-route

Use the `rtl-buddy-pnr` skill for `rb pnr` and `rb pnr-export`. A hardened P&R block reports 0 W in `rb power` and is listed in `unpowered_cells`, so a parent's total leaves out the blocks' own power.

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
