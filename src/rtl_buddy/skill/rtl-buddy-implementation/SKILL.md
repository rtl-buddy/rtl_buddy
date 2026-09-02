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
- Keep named-run YAML and regression manifests distinct. Confirm with
  `rb <flow> --help` rather than copying flags between flows.
- A tool completing does not imply the design met its target. Report timing,
  area, power, routing, and guardrail fields separately from command execution.
- `synth`, `pnr`, `power`, `fpga`, `synth-regression`, `power-regression`, and
  `fpga-regression` exit 0 when every result counts as successful, 1 for any
  `FAIL` or strict `XPASS`, and 2 for a fatal configuration or environment error.
  `SKIP`, `XFAIL`, and non-strict `XPASS` count as successful. XPLR verbs exit 0
  on success and 2 on fatal errors.

## Synthesis correctness gates

`rb synth` (both backends) fails on two silent-corruption shapes before
reporting PPA. A `function`/`task` without an explicit `automatic` lifetime
shares one storage location per formal across call sites; the gate names each
`file:line: function <name>`, following `` `include ``s and honouring
`` `ifdef ``. Fix the RTL by adding `automatic` — do not reach for
`static-functions: allow`. This gate is new and defaults to `error` under
`frontend: slang`, so a previously passing run can now fail; `warn` stages the
migration. Yosys `multiple conflicting drivers` warnings fail the run under
`conflicting-drivers: error` (a tristate bus is exempt); they mean a net folded
to `x` and may have taken registers with it, so never report the area or gate
count from such a run. `static_function_findings` in a passing result means the
gate ran in `warn` mode and the netlist may still be wrong. A failed gate also
deletes the netlist, so `rb pnr` / `rb power` cannot read it.

A `synth.filelist_defines_overridden` warning means the synth.yaml entry's
`defines:` set a macro the model filelist also defines, with a different
value — synthesis elaborates with the synth.yaml value, simulation with the
filelist's. Drop one of the two if the flows are meant to agree.

## FPGA timing closure

`timing_met: false` is a completed result, not necessarily a tool crash. Start
with the worst failing path and `wns_ns`, form one hypothesis, make one focused
RTL/constraint change, rerun, and compare the same metrics. Do not paper over a
CDC or quasi-static path by relaxing the clock.

Use `rb --machine docs show concepts/fpga` for the closure decision tree and
`concepts/power` for completeness/coverage semantics.

## XPLR

XPLR records experiments and maintains the Pareto frontier; it does not propose
the next experiment. Record the hypothesis, rationale, parent, source revision,
knobs, and metric directions, then attach the observed outcome. Use
`rb --machine docs show concepts/xplr` for the manifest contract.
