## Inspect artefacts

Single runs write under `artefacts/<test>/`; repeated runs use `run-NNNN/` subdirectories. Common files are:

- `test.log` and `test.err` — simulator output;
- `test.randseed` — resolved seed;
- `compile.log` — compile output;
- `run.f` — generated non-portable filelist;
- `coverage.dat` — raw coverage when enabled.

Latest-run symlinks for the test log, error log, and seed remain at the test artefact root. `rtl_buddy.log` beside `tests.yaml` contains orchestration events; `--machine` makes it JSON Lines and returns structured stdout. See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/dev/agents/#machine-mode).
