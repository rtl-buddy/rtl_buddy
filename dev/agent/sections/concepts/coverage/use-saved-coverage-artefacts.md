## Use saved coverage artefacts

Every run that produces coverage writes `<command root>/cov_dir/manifest.json`, even without merging. The manifest records the run context, totals, tests, and paths to raw, merged, HTML, dataset, description, Coverview, and model artefacts.

Manifest path fields are POSIX project-relative paths when possible. Stable output blocks remain present and use `null` for artefacts that were not produced. `merge_mode` is `raw`, `info_process`, or `null`.

`cov_dir/coverage-model.json` stores the actionable detail:

- totals and counts by metric;
- files and their modules;
- line, branch, toggle, expression, and cover points;
- hit counts attributed to each test.

Paths are project-relative. Line points are keyed by line; other points use line, column, name, and module because several may share a source line.

Toggle, expression, and labeled cover detail comes from raw Verilator databases. If a raw database is unavailable, the model can fall back to LCOV info for unnamed line and branch data only.
