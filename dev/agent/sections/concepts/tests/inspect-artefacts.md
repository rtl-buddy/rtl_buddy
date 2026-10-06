## Inspect artefacts

A run writes to `artefacts/<test>/`; repeated runs use `run-NNNN/` subdirectories.

- `test.log`, `test.err`: simulator output.
- `test.randseed`: resolved seed.
- `compile.log`: compile output, or the reuse message when nothing compiled.
- `compile.retry.log`: the recompile of a dispatched simulation job whose shared build was stale.
- `run.f`: generated, non-portable filelist.
- `coverage.dat`: raw coverage, when enabled.

Before PRE, a run removes the previous run's `test.log`, `test.err`, `coverage.dat`, `compile.retry.log`, `result.json` and, for cocotb, `cocotb_results.xml`, so a test that stops at setup or compile leaves no earlier verdict beside its own. `test.randseed` is kept for `--replay`. Under `--dispatch` the build job clears the test's directory and each simulation job clears its run's, so a run whose job never started keeps the previous run's files; the summary and `--machine` output always describe this run.

Symlinks to the latest test log, error log and seed stay at the test's artefact root. `rtl_buddy.log` beside `tests.yaml` holds orchestration events; `--machine` makes it JSON Lines and returns structured stdout. See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/dev/agents/#machine-mode).
