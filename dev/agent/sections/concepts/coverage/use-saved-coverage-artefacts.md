## Use saved coverage artefacts

Every run that produces coverage writes `<command root>/cov_dir/manifest.json`, even without merging. The manifest records the run context, totals, tests, and paths to raw, merged, HTML, dataset, description, Coverview, and model artefacts.

Manifest path fields are POSIX project-relative paths when possible. Stable output blocks remain present and use `null` for artefacts that were not produced. `merge_mode` is `raw`, `info_process`, or `null`. `merge_failed` and `failed_metrics` state whether a requested merge survived; see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/#read-a-failed-merge). `source_totals` sits beside `totals` and is `null` for a manifest whose model carried no such figure.

`cov_dir/coverage-model.json` stores the actionable detail:

- totals and counts by metric, per elaboration (`totals`) and per source point (`source_totals`);
- files and their modules;
- line, branch, toggle, expression, and cover points;
- hit counts attributed to each test.

Paths are project-relative. Line points are keyed by line; other points use line, column, name, and module because several may share a source line. Both totals blocks ride on the run, on each test and on each file; `source_totals` is the same points keyed without the module. Adding a key does not bump `schema_version`, so a reader of an older document sees `source_totals` absent, never wrong — and consumers omit the key rather than substituting `totals`.

Toggle, expression, and labeled cover detail comes from raw Verilator databases. If a raw database is unavailable, the model can fall back to LCOV info for unnamed line and branch data only.
