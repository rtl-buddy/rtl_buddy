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
- `pnr-export` runs the KLayout export over a saved P&R result and starts no
  P&R and no synthesis. There the export is the whole job, so any export that
  was not delivered is a `FAIL` — unlike `rb pnr`, where a failed `preview`
  export leaves the P&R verdict standing. A layout published with cells that
  have no GDS stays a qualified pass in `preview` and is a `FAIL` in `strict`.
  Read `gds_status` and `gds_missing_cells` before reporting a layout, and
  `export_provenance` for the record of what was read.
- OpenROAD runs single-threaded unless a `pnr.yaml`, `power.yaml` or
  `synth.yaml` entry sets `threads:` (a count, or `auto` for the allocation).
  A count above a Slurm/affinity allocation is clamped with
  `openroad.threads_capped`; quote `openroad_threads.effective`, not the
  configured value.

## Synthesis correctness gates

For block boundary planning, set `pnr.yaml`'s optional `pin-constraints` Tcl
path relative to that YAML. It runs after floorplan/tracks, before `place_pins`.
Do not place pin-region commands in SDC, which is read before the die exists.
The default without the key remains unconstrained placement.

`rb synth` (both backends) gates three silent-corruption shapes before
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

A `synth.unresolved_interface` warning means `read_verilog` could not bind a
SystemVerilog interface instance to a child's interface port and fell back to
a per-child `<child>$interfaces$<interface>` module. The interface's members
still connect, but the instance's own port connections are dropped — an
interface carrying `clk` or `rst_n` leaves them undriven and the subtree loses
its clock. Check the netlist before quoting PPA from such a run, and prefer
`frontend: slang`, which binds the instance. `unresolved-interfaces: error`
makes it a failed run; the default is `warn` because the fallback is correct
when the interface has no ports of its own, or none the subtree reads.

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
