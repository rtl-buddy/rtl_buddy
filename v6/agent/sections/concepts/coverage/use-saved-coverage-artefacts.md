## Use saved coverage artefacts

Every run that produces coverage writes `<command root>/cov_dir/manifest.json`, even without merging. The manifest records the run context, totals, tests, and paths to raw, merged, HTML, dataset, description, Coverview, and model artefacts.

Manifest path fields are POSIX project-relative paths when possible. Stable output blocks remain present and use `null` for artefacts that were not produced. `merge_mode` is `raw`, `info_process`, or `null`. `merge_failed` and `failed_metrics` state whether a requested merge survived; see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/#read-a-failed-merge). `source_totals` sits beside `totals` and is `null` for a manifest whose model carried no such figure. `coverage_model` records the `--coverage-model` value, and `model` is `null` under `none`; see [Skip the model when nothing will read it](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/#skip-the-model-when-nothing-will-read-it).

`cov_dir/coverage-model.json` stores the actionable detail:

- totals and counts by metric, per elaboration (`totals`) and per source point (`source_totals`);
- files and their modules;
- line, branch, toggle, expression, and cover points;
- hit counts attributed to each test, unless the run used `--coverage-model totals`.

Paths are project-relative. Line points are keyed by line; other points use line, column, name, and module because several may share a source line. Both totals blocks ride on the run, on each test and on each file; `source_totals` is the same points keyed without the module. Adding a key does not bump `schema_version`, so a reader of an older document sees `source_totals` absent, never wrong — and consumers omit the key rather than substituting `totals`.

Toggle, expression, and labeled cover detail comes from raw Verilator databases. If a raw database is unavailable, the model can fall back to LCOV info for unnamed line and branch data only.

### Skip the model when nothing will read it

The per-test attribution grows with points × tests. For a large toggle-instrumented suite it can make the model most of a run's output and most of the time spent after dispatch. `--coverage-model` on `test` and `regression` chooses how much of the model to write:

| Value | `coverage-model.json` | Manifest |
|---|---|---|
| `full` (default) | Every point with its per-test hit counts | `model` names the file |
| `totals` | Every point and hit count, with no per-point `tests` map; `attribution: false` | `model` names the file |
| `none` | Not written; a model left by an earlier run is removed | `model` is `null` |

In every mode the manifest keeps `totals`, `source_totals` and the per-test rows, and records the choice in `coverage_model`. The console summary, merges, `--coverage-dir-summary` and `--coverage-source-summary` are unchanged, because they come from the same pass over the per-test databases. Use `none` for a CI job that records the suite figure and discards its artefacts:

```bash
rb -M cov regression --coverage-merge --coverage-model none
```

`rb cov` and the hub `/cov` pane need a model. Under `totals` they report points and totals with no attribution. Under `none` they exit with an error that names the flag.
