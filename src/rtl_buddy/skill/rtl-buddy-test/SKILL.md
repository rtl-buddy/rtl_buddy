---
name: rtl-buddy-test
description: Run and debug rtl_buddy tests, randtests, and regressions; use for verdicts, timeouts, artefacts, and shared builds.
---

# rtl_buddy tests and regressions

Report `rb --version` at the top of every run summary.

Use `rb --machine` and read `payload.results`, not rendered tables. For syntax and schemas use `rb test --help`, `rb randtest --help`, and `rb --machine docs show concepts/tests` or `concepts/regressions`.

## Invocation and outputs

- Pass `-c path/to/tests.yaml` to make the suite explicit. Check `rb test --help`: use multi-select when available, otherwise loop exact names.
- Config-relative paths and outputs anchor on `dirname(tests.yaml)`. A regression anchors each suite on its `tests.yaml` and its own output on `dirname(regression.yaml)`.
- `rb test --plusarg KEY=VALUE` (repeatable; bare `KEY` for `+KEY`) overrides one runtime plusarg for a single run. It reaches `preproc` and the simulator, survives `--dispatch`, never rebuilds, and is recorded as `plusarg_overrides`.
- Test artefacts are in `artefacts/<test>/`; randtest iterations use `run-NNNN/`. Durable verdicts are in `result.json`; `rtl_buddy.log` is JSONL.
- "another rtl-buddy run is already using this artefact tree" means one lock per artefact tree. To run two tiers at once, give each run `--run-tag <name>`: its tree, lock, log and overlay move under `artefacts/.runs/<name>/`, shared builds stay shared, and `rb graph results --run-tag <name>` converts that run. The compile cwd moves too, so write project files in `compile-time` opts as `${RTL_BUDDY_PROJECT_ROOT}/...`.
- On a long regression, `rb --print-failures-only --machine regression ...` trims `PASS`/`SKIP`/`XFAIL` rows from the console summary. The `summary` event and the log keep every row.

## Verdicts

- UVM uses its report thresholds; cocotb uses `cocotb_results.xml`.
- Other simulations need a line beginning `PASS` or `FAIL` in `test.log`. Follow `FAIL` with `ERR:` or `FAT:` so `desc` holds the reason.
- `payload.results[*].result` and `desc` are authoritative. `NA` means no verdict was produced and needs review; it is not proof of a pass.
- Exit 0: no real `FAIL`, including an `XFAIL` or an intentional early-stop `NA` (only `early_stop: true`, from `-E pre|comp|sim`). Exit 1: a real `FAIL`, an unknown `NA` or a strict `XPASS`. Exit 2: fatal configuration or environment error.

## Reproducible seeds

- Use `--master-seed N` on `rb test` or `rb regression` to replay without old artefacts; rerun the same command.
- For randomized preprocessing, set `sim-rand-seed-plusarg: NAME`. The hook reads that plusarg or `test_cfg.get_resolved_seed()`, and the simulator receives the same value. Use a master or fixed seed, not `--rnd-new`/`--rnd-last`. `randtest` needs a fixed seed because its preprocessor runs once for all iterations.
- A test-level `sim-rand-seed` pins timing-sensitive stimulus and overrides other runtime seed modes.
- Record the master from the summary and the resolved seed from machine results or `test.randseed`. Details: `rb --machine docs show concepts/tests#run-with-randomized-seeds`.

## `Sim hit timeout`

This is rtl_buddy's wall-clock `sim_timeout` kill (default 60 s), not a testbench's simulated-time watchdog. Before raising it:

1. Check whether sibling tests under the same builder pass.
2. Check whether `test.log` timestamps or progress advance.
3. Find the last completed activity, and tell slow progress from a functional wedge.
4. Confirm the resolved timeout.

A recognized VCS `-licqueue` wait pauses the clock; check the reported queue duration before calling a long run a timeout bug. A log ending mid-line, often at a power-of-two size, is a truncated buffer, not where the DUT stopped.

## Memory

Verilator elaboration of large generated structures can be OOM-killed. A Slurm `OUT_OF_MEMORY` state, or a local compiler `Killed`/SIGKILL, calls for more memory, not a longer timeout. When queued, use the `rtl-buddy-dispatch` skill. A misspelt key in a `resources:` or `compile:` block (`memory:`) is ignored with WARNING `config.unknown_key` naming the nearest key, so the job gets the inherited value; a later major makes it fatal.

## Shared builds

`--share-build` reuses a build only for identical compile inputs. Source or header changes, filelists, plusdefines, compile options, configured extra compile environment, builder or toolchain changes rebuild. Plusargs, seeds and `sim_timeout` do not.

- Every builder's stamp lists each `+incdir+` tree (recursively) and `-y` directory (flat). Verilator reports the files it consumed, so its listing is compared by name only: an added or removed file rebuilds, an edit rebuilds only if the build read that file.
- VCS/Icarus report no header dependencies, so any edited, added or removed file in a listed directory rebuilds.
- A header a `preproc` hook writes into `artifact_dir` is tracked. Inside a dispatch build job, configs with one key adopt the first one's build; a consumed input that differs fails that config with `build_job.group_input_drift`, and a new `-y` file or shadowing header declines adoption.
- The dispatch build-job reservation counts each test with a `preproc` hook as its own build, since the hook may set plusdefines. Set `preproc-sets-plusdefines: false` (boolean, on the test or as the suite default) when the hook only writes stimulus; a hook that changes the compile key anyway logs `build_job.preproc_changed_compile_key`.
- Reuse is reported. If an edit seems ignored or a PASS looks suspicious, read `compile.build_reused` (run log; console once per build directory) and the test's `compile.log` breadcrumb. Force a fresh compile with `--rebuild`, not by deleting `artefacts/.shared-builds/`. `--dispatch` implies `--share-build`, so dropping the flag there does not stop reuse.
- `shared-build-root` (config `cfg-rtl-reg`, env `RTL_BUDDY_SHARED_BUILD_ROOT`, flag `--shared-build-root`; flag wins, then env; empty turns it off) keeps builds across workspace wipes, keyed by content so identical inputs share a directory. Nothing prunes it; prune between runs, never during one.

Batch compile-input edits before an expensive build. Details: `rb --machine docs show concepts/tests#sharing-compiled-builds-across-tests`, `concepts/tests#persistent-build-cache` and `known-issues`.
