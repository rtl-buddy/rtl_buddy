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
