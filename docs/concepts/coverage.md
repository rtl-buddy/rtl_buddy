---
description: Collect Verilator coverage, merge and export results, and inspect saved coverage by file, module, point, and test.
---

# Coverage

rtl_buddy collects Verilator coverage during tests, can merge results across a run, and writes a structured model for CLI, machine, MCP, and hub queries.

## Enable coverage

Coverage instrumentation must be present at compile time. Add a builder mode in `root_config.yaml` and select it when running tests:

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

`cfg-coverage.name` must match the simulator family. `use-lcov: true` enables LCOV conversion and HTML generation. Configure optional Coverview packaging under `cfg-coverview`; see [YAML formats](../reference/yaml.md#root_configyaml).

Any coverage output flag asserts that executed tests will produce raw coverage. If no non-skipped test does, the command exits 2 with a configuration error. A selection containing only skipped tests does not error.

## Merge and export results

Choose at most one merge mode:

| Flag | Processing | Supported outputs |
|---|---|---|
| `--coverage-merge` | Raw merge for summary/HTML; info-process for Coverview | Summary, HTML, Coverview |
| `--coverage-merge-raw` | Raw Verilator merge | Summary, HTML, Coverview |
| `--coverage-merge-info-process` | info-process only | Summary, Coverview; no HTML |

Without a merge flag, coverage remains per test.

```bash
rb -M cov regression --coverage-merge --coverage-html
rb -M cov regression --coverage-merge --coverage-coverview
rb -M cov regression --coverage-coverview --coverage-per-test
```

HTML requires `use-lcov: true` and `genhtml`; diagnose it with `rb tool-check --explain lcov`. Output is written to `coverage_merge.html` under the command root.

Coverview is an optional archive export for CI or handoff. Use `rb cov` or the hub coverage pane for interactive inspection; Coverview rendering depends on external `info-process` and compatible Coverview tooling.

### Read a failed merge

The one-line summary uses two different tokens for a missing number:

| Token | Meaning |
|---|---|
| `UNSP` | The metric was never measured: not instrumented, or not representable in the artefact the number was read from. An LCOV `.info` carries no toggle, expression, or functional coverage. |
| `FAIL` | The metric was measured and the measurement was lost: the tool run that was its only source failed. |

`verilator_coverage --write` is the only source for toggle, expression, and functional coverage under `--coverage-merge` and `--coverage-merge-raw`. When it fails — killed by the environment, out of memory, or exiting non-zero — the per-test LCOV exports still succeed, so line and branch report normally and the rest reads `FAIL`:

```text
Merged Coverage: L:0.92 B:0.95 T:FAIL F:FAIL
Coverage merge FAILED: verilator_coverage --write wrote no merged database, so toggle, expression, functional read FAIL (measurement lost), not UNSP (not instrumented) — see the coverage.merge.failed event
```

The run then **exits 1**. Results, side-cars, the coverage model, and the manifest are all written first, so nothing the run produced is lost; only the status reports that the requested measurement is incomplete. `coverage.merge.failed` carries the tool's return code and output, and `coverage.merge.degraded` records the escalation.

Machine and artefact consumers read the same fact explicitly:

- `payload.coverage.merge_failed` (bool) and `payload.coverage.failed_metrics` (metric names) on `test` and `regression`;
- `merge_failed` and `failed_metrics` at the top level of `cov_dir/manifest.json`, and in the `payload.coverage.artefacts` block beside it;
- `merge_failed` and `failed_metrics` on `rb cov summary` and `rb cov module` payloads.

Both keys are always present, so an absent key never means "the merge was fine". Under `merge_mode: "raw"`, `merged.raw` is also `null` when the merge produced nothing — a signal a consumer can still use, but no longer has to infer.

Manifest `totals` is deliberately unchanged by a failed merge. It is computed from the per-test databases rather than the merged one, so it remains a real measurement of what those databases hold; blanking a metric there would discard data the run did produce. A failed merge costs the *merged summary* number, which is why the verdict sits beside `totals` instead of inside it. Compare `merge_failed` before comparing manifest totals against a console summary.

Add directory rollups with repeatable repo-relative prefixes or a file containing one prefix per line:

```bash
rb -M cov regression --coverage-merge \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem

rb -M cov regression --coverage-merge \
  --coverage-dir-summary-file coverage_dirs.txt
```

See the [CLI reference](../reference/cli.md) for the complete option set.

## Inspect cover-property hits

For Verilator, machine output includes each labeled user cover point as `{name, file, line, module, hits}` on the test result and in the run-level aggregate. This data comes from per-test `coverage.dat` and does not require a merge flag.

Verilator folds repeated instances of one point within a module. rtl_buddy then combines tests by `(file, line, name, module)`. The module remains part of the identity so the same included property compiled into different modules is not mistaken for one covered point.

Other simulator families omit the field. Omitted means not collected, not zero coverage.

## Use saved coverage artefacts

Every run that produces coverage writes `<command root>/cov_dir/manifest.json`, even without merging. The manifest records the run context, totals, tests, and paths to raw, merged, HTML, dataset, description, Coverview, and model artefacts.

Manifest path fields are POSIX project-relative paths when possible. Stable output blocks remain present and use `null` for artefacts that were not produced. `merge_mode` is `raw`, `info_process`, or `null`. `merge_failed` and `failed_metrics` state whether a requested merge survived; see [Read a failed merge](#read-a-failed-merge).

`cov_dir/coverage-model.json` stores the actionable detail:

- totals and counts by metric;
- files and their modules;
- line, branch, toggle, expression, and cover points;
- hit counts attributed to each test.

Paths are project-relative. Line points are keyed by line; other points use line, column, name, and module because several may share a source line.

Toggle, expression, and labeled cover detail comes from raw Verilator databases. If a raw database is unavailable, the model can fall back to LCOV info for unnamed line and branch data only.

## Query saved coverage with `rb cov`

`rb cov` reads existing artefacts and writes nothing. Without `--cov-dir`, it selects the newest `cov_dir/manifest.json` under the project root.

```bash
rb cov summary
rb cov summary --limit 0
rb cov module blk
rb cov module blk --all
rb cov summary --cov-dir verif/blk/cov_dir
```

- `summary` reports run and test totals plus the coldest files. `--limit 0` shows all files.
- `module` reports points for exactly the recorded module. `--all` includes hit points as well as misses.

An unknown module exits 2 and reports close candidates. A file shared by modules is filtered to the requested module's points.

Machine payloads include the manifest, run metadata, totals, artefact paths, and verb-specific file, module, test, and point data. The same artefact block is included in machine output from the producing `test` or `regression` command.

`rb mcp` exposes the same query builders as `cov_summary` and `cov_module`. They read files directly and do not require a running hub. See [The MCP server](graph.md#the-mcp-server).

## Inspect coverage in the hub

Start the viewer service and open `/cov`:

```bash
rb hub start --serve-viewer
```

The pane shows totals, metric-ranked files, source annotations, individual points, and per-test attribution from the same model used by `rb cov`. Line selections can focus source and schematic views; module selections can focus the graph. See [Coverage pane](hub.md#coverage-pane).

After `rb graph results`, the design graph also correlates declared `covers:` relationships with observed coverage and reports exercised, declared-only, and observed-but-undeclared items. See [Coverage on the graph](graph.md#coverage-on-the-graph).
