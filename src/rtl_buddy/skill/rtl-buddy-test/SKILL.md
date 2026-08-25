---
name: rtl-buddy-test
description: Run and debug rtl_buddy simulation tests, randomized tests, and regressions, including result parsing, timeout triage, artefact locations, and shared-build behavior.
---

# rtl_buddy tests and regressions

Report `rb --version` at the top of every run summary.

Use `rb --machine`; read `payload.results`, not rendered tables. For complete
syntax and schemas, use `rb test --help`, `rb randtest --help`, and
`rb --machine docs show concepts/tests` or `concepts/regressions`.

## Invocation and outputs

- Pass `-c path/to/tests.yaml` to make the suite explicit. Inspect
  `rb test --help`: use multi-select when available, otherwise loop exact names.
- Config-relative paths and outputs anchor on `dirname(tests.yaml)`.
- A regression anchors each suite on its `tests.yaml`; its orchestration output
  anchors on `dirname(regression.yaml)`.
- Test artefacts are under `artefacts/<test>/`; randtest iterations use
  `run-NNNN/`. Durable verdicts live in `result.json`; `rtl_buddy.log` is JSONL.

## Verdicts

- UVM uses its report thresholds; cocotb uses `cocotb_results.xml`.
- Other simulations need a line beginning `PASS` or `FAIL` in `test.log`.
  Follow `FAIL` with `ERR:` or `FAT:` so `desc` contains the reason.
- Treat `payload.results[*].result` and `desc` as authoritative. `NA` means no
  real verdict was produced and needs review; it is not proof of a pass.

## `Sim hit timeout`

This is rtl_buddy's wall-clock `sim_timeout` kill. It is distinct from a
testbench's simulated-time watchdog. Before raising it:

1. Check whether sibling tests under the same builder pass.
2. Check `test.log` timestamps/progress to see whether simulated time advances.
3. Identify the last completed activity and distinguish slow progress from a
   functional wedge.
4. Confirm the resolved timeout; an omitted `sim_timeout` defaults to 60 s.

A killed process may not flush its final output. A log ending mid-line, often at
a power-of-two size, is a truncated buffer—not the point where the DUT stopped.

## Memory and shared builds

Verilator elaboration of large generated structures can be OOM-killed. A Slurm
`OUT_OF_MEMORY` state or local compiler `Killed`/SIGKILL calls for more memory,
not a longer simulation timeout; use the `rtl-buddy-dispatch` skill when queued.

`--share-build` reuses only identical compile inputs. Tracked/reported source or
header inputs, filelists, plusdefines, compile options, configured extra compile
environment, builder, or toolchain changes rebuild; runtime plusargs, seeds, and
`sim_timeout` do not.
VCS/Icarus may not report header dependencies. Batch compile-input edits before
an expensive build and use independent cheap suites while it runs.

Read `rb --machine docs show known-issues` before forcing cache deletion.
