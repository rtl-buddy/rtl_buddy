---
name: rtl-buddy-test
description: Run and debug rtl_buddy tests, randtests, and regressions; use for verdicts, timeouts, artefacts, and shared builds.
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
- `test`, `randtest`, and `regression` exit 0 with no real `FAIL` (including an
  intentional `NA` or `XFAIL`), 1 for a real `FAIL` or strict `XPASS`, and 2 for
  a fatal configuration or environment error.

## `Sim hit timeout`

This is rtl_buddy's wall-clock `sim_timeout` kill. It is distinct from a
testbench's simulated-time watchdog. Before raising it:

1. Check whether sibling tests under the same builder pass.
2. Check `test.log` timestamps/progress to see whether simulated time advances.
3. Identify the last completed activity and distinguish slow progress from a
   functional wedge.
4. Confirm the resolved timeout; an omitted `sim_timeout` defaults to 60 s.

A recognized VCS `-licqueue` wait pauses the `sim_timeout` clock. Check the
reported queue duration before treating a visibly long run as a timeout bug.

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
Every builder's stamp lists each `+incdir+` tree (recursively) and `-y`
directory (flat), unfiltered by suffix. Verilator reports which files it
consumed, so for it the listing is compared by name only: an added or removed
file rebuilds, an edit rebuilds only if the build read that file (headers
included, via the dependency list, which follows symlinks on every check).
VCS/Icarus report no dependencies, so for them any edited, added, or removed
file in a listed directory rebuilds. The walk skips dot-directories,
`artefacts`/`obj_dir*`, editor/VCS bookkeeping, rtl_buddy's own outputs by
name (run.f, compile.log, test.log, result.json, the stamp), and the suite's own
`rtl_buddy.log` by path; a header generated into a test's `artifact_dir` by a
preproc hook is still tracked.
Batch compile-input edits before
an expensive build and use independent cheap suites while it runs.

Reuse is reported, not silent: when an edit seems not to take effect or a PASS
looks suspicious after one, read the `compile.build_reused` line (in the run's own log; on the console
once per build directory) and the test's `compile.log` breadcrumb, which name the reused build directory and its
stamp's age. `--rebuild` then forces a fresh compile; use it instead of deleting
`artefacts/.shared-builds/`, and note that `--dispatch` implies `--share-build`,
so dropping the flag there does not stop reuse. Read
`rb --machine docs show known-issues` for the remaining limits.
