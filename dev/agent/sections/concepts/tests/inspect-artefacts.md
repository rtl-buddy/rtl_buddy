## Inspect artefacts

A run writes to `artefacts/<test>/`; repeated runs use `run-NNNN/` subdirectories.

- `test.log`, `test.err`: simulator output.
- `test.randseed`: resolved seed.
- `compile.log`: compile output, or the reuse message when nothing compiled.
- `compile.retry.log`: the recompile of a dispatched simulation job whose shared build was stale.
- `run.f`: generated, non-portable filelist.
- `coverage.dat`: raw coverage, when enabled.

Symlinks to the latest test log, error log and seed stay at the test's artefact root. `rtl_buddy.log` beside `tests.yaml` holds orchestration events; `--machine` makes it JSON Lines and returns structured stdout. See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/dev/agents/#machine-mode).
