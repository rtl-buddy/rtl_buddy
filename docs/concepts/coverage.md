---
description: Collect Verilator coverage, merge and export results, and inspect saved coverage by file, module, point, and test.
---

# Coverage

rtl_buddy collects Verilator coverage during tests, can merge results across a run, and writes a structured model for CLI, machine, MCP, and hub queries.

## Enable coverage

Coverage instrumentation must be compiled in. Add a builder mode and a `cfg-coverage` entry in `root_config.yaml`, then select the mode with `-M`:

```yaml
cfg-rtl-builder:
  - name: verilator
    builder: verilator
    builder-simv: obj_dir/simv
    builder-opts:
      cov:
        compile-time: --binary -sv -o simv --coverage
        run-time: +verilator+rand+reset+2

cfg-coverage:
  - name: verilator
    use-lcov: true
```

```bash
rb -M cov test basic
rb -M cov regression
```

`cfg-coverage.name` must match the simulator family. `use-lcov: true` enables LCOV conversion and HTML generation. Optional Coverview packaging is configured under `cfg-coverview`; see [YAML formats](../reference/yaml.md#root_configyaml).

Any coverage output flag asserts that the executed tests will produce raw coverage. If no non-skipped test does, the command exits 2 with a configuration error. A selection containing only skipped tests is not an error.

## Merge and export results

Choose at most one merge mode. Without one, coverage stays per test.

| Flag | Processing | Supported outputs |
|---|---|---|
| `--coverage-merge` | Raw merge for summary/HTML; info-process for Coverview | Summary, HTML, Coverview |
| `--coverage-merge-raw` | Raw Verilator merge | Summary, HTML, Coverview |
| `--coverage-merge-info-process` | info-process only | Summary, Coverview; no HTML |

```bash
rb -M cov regression --coverage-merge --coverage-html
rb -M cov regression --coverage-merge --coverage-coverview
rb -M cov regression --coverage-coverview --coverage-per-test
```

HTML needs `use-lcov: true` and `genhtml`; diagnose with `rb tool-check --explain lcov`. It is written to `coverage_merge.html` under the command root.

Coverview is an optional archive export for CI or handoff and needs the external `info-process` and compatible Coverview tooling. For interactive inspection use `rb cov` or the hub coverage pane. See the [CLI reference](../reference/cli.md) for the full option set.

## Add directory and source-point summaries

`--coverage-dir-summary` adds rollups for repo-relative directory prefixes. Repeat the flag, or list one prefix per line in a file:

```bash
rb -M cov regression --coverage-merge \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem

rb -M cov regression --coverage-merge \
  --coverage-dir-summary-file coverage_dirs.txt
```

`--coverage-source-summary` adds the run's source-point figures, which differ from the default per-elaboration ones (see [Per-elaboration vs source-point figures](#per-elaboration-vs-source-point-figures)):

```bash
rb -M cov regression --coverage-merge --coverage-source-summary
```

## Read a failed merge

The one-line summary uses two tokens for a missing number:

| Token | Meaning |
|---|---|
| `UNSP` | The metric was never measured: not instrumented, or not representable in the artefact it was read from. An LCOV `.info` has no toggle, expression, or functional coverage. |
| `FAIL` | The metric was measured and the measurement was lost because the only tool run that produced it failed. |

Under `--coverage-merge` and `--coverage-merge-raw`, `verilator_coverage --write` is the only source of toggle, expression, and functional coverage. If it is killed, runs out of memory, or exits non-zero, those metrics read `FAIL`. Line and branch still report when `use-lcov` or `--coverage-html` is on, because they come from per-test LCOV exports. Otherwise they come from the merged database and also read `FAIL`:

```text
Merged Coverage: L:0.92 B:0.95 T:FAIL F:FAIL
Coverage merge FAILED: verilator_coverage --write wrote no merged database, so toggle, expression, functional read FAIL (measurement lost), not UNSP (not instrumented) — see the coverage.merge.failed event
```

The run exits 1. Results, side-cars, the coverage model, and the manifest are written first, so nothing is lost. The `coverage.merge.failed` event carries the tool's return code and output, and `coverage.merge.degraded` records the escalation.

Consumers read the failure from these keys, which are always present:

- `payload.coverage.merge_failed` (bool) and `payload.coverage.failed_metrics` on `test` and `regression`.
- `merge_failed` and `failed_metrics` at the top level of `cov_dir/manifest.json` and in `payload.coverage.artefacts`.
- `merge_failed` and `failed_metrics` in `rb cov summary` and `rb cov module` payloads.

Under `merge_mode: "raw"`, `merged.raw` is also `null` when the merge produced nothing.

Manifest `totals` is unaffected by a failed merge: it is computed from the per-test databases and stays a real measurement. Check `merge_failed` before comparing manifest totals with a console summary.

## Per-elaboration vs source-point figures

Verilator keys each coverage point by the module it elaborated, so one source point is recorded once per parameterisation. In a suite whose compile keys build the same RTL under different defines, a key that exercises none of a block leaves that block's copy uncovered whatever the rest of the suite did. Example from a seven-key suite:

| Metric | Source points | Per elaboration |
|---|---|---|
| line | 355/399 (89.0%) | 760/924 (82.3%) |
| branch | 351/374 (93.9%) | 792/954 (83.0%) |
| toggle | 140715/145692 (96.6%) | 290437/313312 (92.7%) |

rtl_buddy reports both figures:

- **Per elaboration** (`totals`) answers "is this point covered in every build". Use it when one compile key is what you care about.
- **Source point** (`source_totals`) answers "is this point covered by the suite", which is how a closure target is normally stated. A point counts as covered when any elaboration hit it.

A source point is identified by file, metric, line, column, and point description. Only the elaborated module is dropped. Hit counts are summed, `found` counts distinct collapsed points, and `hit` counts those with any hits. A point no elaboration hit stays a miss.

Line points are keyed by line alone within a file, so line figures are already collapsed: the `run` and `run (source)` rows of `rb cov summary` agree on `line` and differ on branch, toggle, expression, and cover. A per-test row counts one line record per elaboration, and its own `source_totals` collapses them.

## Find the source-point figures

The two figures appear on these surfaces:

| Surface | Figure |
|---|---|
| `rb cov summary` | `run` and `run (source)` rows; `--by-source` ranks the coldest files by source point |
| `rb --machine cov summary`, `rb mcp` `cov_summary` | `source_totals` beside `totals`, per run, per test, and per file |
| `test` / `regression` `--coverage-source-summary` | `Coverage source points <metric>:` lines and `coverage.source_summary` |
| `cov_dir/manifest.json`, `cov_dir/coverage-model.json` | `source_totals` beside `totals` |
| `--coverage-dir-summary` | per elaboration only |

The directory summary is parsed from LCOV, which has already folded elaborations together and dropped point names, so it has no source-point form.

The source summary comes from the coverage model, built from the per-test raw `.dat` databases, which are the only input that records the module per point. A run with only LCOV `.info` fallback records no module, so its two figures are equal. If the run produced no model, the summary prints `Coverage source points: unavailable (no coverage model)` instead of zeros.

## Inspect cover-property hits

For Verilator, machine output lists each labeled user cover point as `{name, file, line, module, hits}` on the test result and in the run-level aggregate. The data comes from the per-test `coverage.dat`; no merge flag is needed.

Verilator folds repeated instances of one point within a module. rtl_buddy then combines tests by `(file, line, name, module)`, so the same included property compiled into different modules stays separate.

Other simulator families omit the field. Omitted means not collected, not zero coverage.

## Use saved coverage artefacts

Every coverage run writes `<command root>/cov_dir/manifest.json`, even without merging. It records the run context, totals, tests, and paths to the raw, merged, HTML, dataset, description, Coverview, and model artefacts.

- Paths are POSIX project-relative where possible.
- Output blocks are always present; artefacts that were not produced are `null`.
- `merge_mode` is `raw`, `info_process`, or `null`.
- `merge_failed` and `failed_metrics` report whether a requested merge survived; see [Read a failed merge](#read-a-failed-merge).
- `source_totals` sits beside `totals` and is `null` when the model has no such figure.
- `coverage_model` records the `--coverage-model` value, and `model` is `null` under `none`; see [Skip the model when nothing will read it](#skip-the-model-when-nothing-will-read-it).

`cov_dir/coverage-model.json` holds the detail:

- totals and counts by metric, per elaboration (`totals`) and per source point (`source_totals`), on the run, on each test, and on each file;
- files and their modules;
- line, branch, toggle, expression, and cover points, with project-relative paths;
- hit counts per test, unless the run used `--coverage-model totals`.

Line points are keyed by line. Other points use line, column, name, and module, since several can share a source line. Readers of older documents see `source_totals` absent, never wrong; consumers omit the key rather than substituting `totals`.

Toggle, expression, and labeled cover detail comes from raw Verilator databases. Without one, the model falls back to LCOV info and holds only unnamed line and branch data.

## Skip the model when nothing will read it

Per-test attribution grows with points times tests. For a large toggle-instrumented suite it can dominate a run's output and its post-dispatch time. `--coverage-model` on `test` and `regression` chooses how much to write:

| Value | `coverage-model.json` | Manifest |
|---|---|---|
| `full` (default) | Every point with its per-test hit counts | `model` names the file |
| `totals` | Every point and hit count, with no per-point `tests` map; `attribution: false` | `model` names the file |
| `none` | Not written; a model left by an earlier run is removed | `model` is `null` |

In every mode the manifest keeps `totals`, `source_totals`, and the per-test rows, and records the choice in `coverage_model`. The console summary, merges, `--coverage-dir-summary`, and `--coverage-source-summary` are unchanged. Use `none` for a CI job that records the suite figure and discards its artefacts:

```bash
rb -M cov regression --coverage-merge --coverage-model none
```

`rb cov` and the hub `/cov` pane need a model. Under `totals` they show points and totals without attribution. Under `none` they exit with an error that names the flag.

## Query saved coverage with `rb cov`

`rb cov` reads existing artefacts and writes nothing. Without `--cov-dir` it uses the newest `cov_dir/manifest.json` under the project root.

```bash
rb cov summary
rb cov summary --limit 0
rb cov module blk
rb cov module blk --all
rb cov summary --cov-dir verif/blk/cov_dir
rb cov summary --by-source
```

- `summary` reports run and test totals and the coldest files. `--limit 0` shows all files. The totals table has a `run` row and a `run (source)` row. `--by-source` ranks the coldest files by source point and lists the same files in the same order.
- `module` reports points for exactly the recorded module; `--all` includes hit points as well as misses. Module figures are per elaboration, since a model module is one elaboration.

An unknown module exits 2 and lists close candidates. A file shared by several modules is filtered to the requested module's points.

Machine payloads include the manifest, run metadata, totals, artefact paths, and verb-specific file, module, test, and point data. Both totals blocks are always present, with or without `--by-source`. The producing `test` or `regression` command's machine output includes the same artefact block.

`rb mcp` exposes the same builders as `cov_summary` and `cov_module`. They read files directly and need no hub. See [The MCP server](graph.md#the-mcp-server).

## Inspect coverage in the hub

```bash
rb hub start --serve-viewer
```

Open `/cov`. The pane shows totals, metric-ranked files, source annotations, individual points, and per-test attribution from the same model as `rb cov`. Its figures are per elaboration; the source-point percentages are in the header tooltip. Line selections focus the source and schematic views, and module selections focus the graph. See [Coverage pane](hub.md#coverage-pane).

After `rb graph results`, the design graph also joins declared `covers:` relationships to observed coverage. See [Coverage on the graph](graph.md#coverage-on-the-graph).
