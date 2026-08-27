## Inspect artefacts

Single runs write under `artefacts/<test>/`; repeated runs use `run-NNNN/` subdirectories. Common files are:

- `test.log` and `test.err` — simulator output;
- `test.randseed` — resolved seed;
- `compile.log` — compile output;
- `compile.retry.log` — output of a dispatched simulation job's recompile, written only when that job was gated on a build job whose stamp did not validate. Run-scoped: it lives in the run's own directory (`run-NNNN/` for a fanned-out test), because only the run that retried wrote it. It never replaces `compile.log`, which stays the build job's own record;
- `run.f` — generated non-portable filelist;
- `coverage.dat` — raw coverage when enabled.

Latest-run symlinks for the test log, error log, and seed remain at the test artefact root. `rtl_buddy.log` beside `tests.yaml` contains orchestration events; `--machine` makes it JSON Lines and returns structured stdout. See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/v6/agents/#machine-mode).
