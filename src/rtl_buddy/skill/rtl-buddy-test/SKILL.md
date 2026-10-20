---
name: rtl-buddy-test
description: Run and debug rtl_buddy tests, randtests, and regressions; use for verdicts, timeouts, artefacts, and shared builds.
---

# rtl_buddy tests and regressions

Report `rb --version` at the top of every run summary. Use `rb --machine` and read `payload.results`. For syntax, use command help; for procedures, use `rb --machine docs show concepts/tests` or `concepts/regressions`.

## Invoke and locate results

- Use `-c path/to/tests.yaml` for an explicit suite. Check `rb test --help`: use multi-select when available, otherwise loop exact names.
- Config-relative paths and outputs anchor on the config's directory. Regressions keep each suite's outputs there and merged outputs beside the manifest.
- `--plusarg KEY=VALUE` (repeatable; bare `KEY` for `+KEY`) overrides a runtime plusarg without rebuilding. It reaches preprocessing and dispatched workers and is recorded as `plusarg_overrides`.
- Test artefacts live in `artefacts/<test>/`, randomized iterations in `run-NNNN/`. Read durable verdicts in `result.json` and events in JSONL `rtl_buddy.log`.
- Concurrent runs need distinct `--run-tag` values: trees, locks, logs, and overlays move under `artefacts/.runs/<tag>/`; shared builds stay shared. Convert results with `rb graph results --run-tag <tag>`. Compile CWD also moves, so reference project files as `${RTL_BUDDY_PROJECT_ROOT}/...` in compile options.
- Machine stderr hides `PASS`, `SKIP`, and `XFAIL` rows but keeps the full tally. Stdout results and the log's `summary` event retain all rows. Human mode offers `--print-failures-only`.

## Interpret verdicts

- UVM uses report thresholds; cocotb uses `cocotb_results.xml`. Other simulations need a line beginning `PASS` or `FAIL` in `test.log`. Follow `FAIL` with `ERR:` or `FAT:` to supply the reason.
- `payload.results[*].result` and `desc` are authoritative. `NA` needs investigation; it proves no pass.
- Negative controls need `xfail_strict: true` and `xfail_match: '<regex>'`. Matching checks the first `ERR:`/`FAT:` line, then `desc`; another failure, including a watchdog, stays `FAIL`.
- Exit 0: no real failure, including `XFAIL` or intentional early-stop `NA` (`early_stop: true`, from `-E pre|comp|sim`). Exit 1: real `FAIL`, unknown `NA`, or strict `XPASS`. Exit 2: fatal configuration or environment error.

## Reproduce seeds

- Replay `test` or `regression` with the same `--master-seed N` command, without needing old artefacts.
- Randomized preprocessing needs `sim-rand-seed-plusarg: NAME`. Read it through `test_cfg.get_plusarg(NAME)` or `get_resolved_seed()`; the simulator gets the same seed. Use a master or fixed seed. `randtest` needs a fixed seed because preprocessing runs once for all iterations.
- Test-level `sim-rand-seed` overrides other runtime seed modes. Record the master and resolved seed from results or `test.randseed`.

Details: `concepts/tests#run-with-randomized-seeds`.

## Diagnose timeouts and memory

`Sim hit timeout` is the wall-clock `sim_timeout` kill (default 60 s). Before increasing it, compare sibling tests, check log progress, locate the last completed activity, and confirm the resolved timeout. Distinguish slow work from a functional wedge.

A recognized VCS `-licqueue` wait pauses the clock; check queue duration. A log ending mid-line can be an unflushed buffer and does not locate the DUT's stop.

Slurm `OUT_OF_MEMORY` or compiler `Killed`/SIGKILL needs more memory. Large generated structures often peak during Verilator elaboration; raising the simulation timeout cannot help. Use `rtl-buddy-dispatch` for reservations. ERROR `config.unknown_key_rejected` (exit 2) requires correcting every reported reservation key, such as `memory:` to `mem:`.

## Reuse and rebuild

`--share-build` reuses identical compile inputs. Sources, headers, filelists, plusdefines, compile options, configured extra compile environment, builders, and toolchains can invalidate it; runtime plusargs, seeds, and timeouts do not. Batch edits before expensive compilation.

- Verilator tracks consumed files plus directory listings: additions/removals rebuild; an edit rebuilds only if consumed. VCS/Icarus report no header dependencies, so they track all files in listed include/library directories. See `concepts/tests#sharing-compiled-builds-across-tests` for limits.
- Preprocessor outputs in `artifact_dir` are tracked. Dispatched same-key configs reuse the first build; differing consumed inputs fail with `build_job.group_input_drift`. New library files or shadowing headers decline adoption.
- Set `preproc-sets-plusdefines: false` on stimulus-only hooks to avoid reserving one build per test. A hook that changes the key anyway logs `build_job.preproc_changed_compile_key`.
- Inspect `compile.build_reused` and `compile.log` for reused directories and stamp ages. Force compilation with `--rebuild`; dispatch implies sharing even without `--share-build`.
- A persistent `shared-build-root` survives workspace wipes. CLI overrides environment, then config; empty disables it. Entries never expire automatically; prune only between runs. See `concepts/tests#persistent-build-cache`.
- `cfg-rtl-reg.shared-build-prune: true` removes Verilator intermediates after new successful shared builds, preserving executable and stamp reuse. Disable it and use `--rebuild` to debug generated C++. See `concepts/tests#pruning-verilator-intermediates`.
