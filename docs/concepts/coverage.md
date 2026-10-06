---
description: Collect Verilator coverage, merge and export results, and inspect saved coverage by file, module, point, and test.
---

# Coverage

rtl_buddy collects Verilator coverage during tests, can merge it across a run, and saves a structured model that `rb cov`, machine output, MCP and the hub read.

```bash
rb -M cov regression --coverage-merge --coverage-html
rb cov summary
```

## Enable coverage

Coverage must be compiled in. Add a builder mode and a `cfg-coverage` entry to `root_config.yaml`, then select the mode with `-M`:

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

`cfg-coverage.name` must match the simulator family. `use-lcov: true` enables LCOV conversion and HTML generation. Coverview packaging is configured under `cfg-coverview`; see [YAML formats](../reference/yaml.md#root_configyaml).

## Merge and export results

Choose at most one merge mode. Without one, coverage stays per test.

| Flag | Processing | Outputs |
|---|---|---|
| `--coverage-merge` | Raw merge for summary and HTML; info-process for Coverview | Summary, HTML, Coverview |
| `--coverage-merge-raw` | Raw Verilator merge | Summary, HTML, Coverview |
| `--coverage-merge-info-process` | info-process only | Summary, Coverview; no HTML |

```bash
rb -M cov regression --coverage-merge --coverage-html
rb -M cov regression --coverage-merge --coverage-coverview
rb -M cov regression --coverage-coverview --coverage-per-test
```

HTML needs `use-lcov: true` and `genhtml` (diagnose with `rb tool-check --explain lcov`) and is written to `coverage_merge.html` under the command root. Coverview is an archive export for CI or handoff and needs the external `info-process` and compatible Coverview tooling. For interactive inspection use `rb cov` or the hub. See the [CLI reference](../reference/cli.md) for all options.

## Add directory and source-point summaries

`--coverage-dir-summary` adds rollups for repo-relative directory prefixes. Repeat the flag, or list one prefix per line in a file:

```bash
rb -M cov regression --coverage-merge \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem

rb -M cov regression --coverage-merge \
  --coverage-dir-summary-file coverage_dirs.txt
```

`--coverage-source-summary` adds source-point figures, which differ from the default per-elaboration ones (see [Per-elaboration vs source-point figures](#per-elaboration-vs-source-point-figures)):

```bash
rb -M cov regression --coverage-merge --coverage-source-summary
```

## Read a failed merge

The one-line summary uses two tokens for a missing number:

| Token | Meaning |
|---|---|
| `UNSP` | The metric was never measured: not instrumented, or not representable in the source. An LCOV `.info` has no toggle, expression or functional coverage. |
| `FAIL` | The metric was measured, but the only tool run that produced it failed. |

Under `--coverage-merge` and `--coverage-merge-raw`, `verilator_coverage --write` is the only source of toggle, expression and functional coverage. If it is killed, runs out of memory or exits non-zero, those metrics read `FAIL`. Line and branch still report when `use-lcov` or `--coverage-html` is on; otherwise they read `FAIL` too.

```text
Merged Coverage: L:0.92 B:0.95 T:FAIL F:FAIL
Coverage merge FAILED: verilator_coverage --write wrote no merged database, so toggle, expression, functional read FAIL (measurement lost), not UNSP (not instrumented) — see the coverage.merge.failed event
```

The run exits 1, even when every test passed. Results and saved coverage are still written, and the `coverage.merge.failed` event carries the tool's return code and output. If the cause is memory or a killed process, rerun the coverage command on a compute node instead of the submit host. Machine output reports the failure as `merge_failed`.

## Per-elaboration vs source-point figures

Verilator records each coverage point once per elaborated module, so one source point appears once per parameterisation. In a suite whose compile keys build the same RTL under different defines, a key that exercises none of a block leaves that block's copy uncovered whatever the rest of the suite did. Example from a seven-key suite:

| Metric | Source points | Per elaboration |
|---|---|---|
| line | 355/399 (89.0%) | 760/924 (82.3%) |
| branch | 351/374 (93.9%) | 792/954 (83.0%) |
| toggle | 140715/145692 (96.6%) | 290437/313312 (92.7%) |

rtl_buddy reports both:

- **Per elaboration** (`totals`) answers "is this point covered in every build". Use it when one compile key is what you care about.
- **Source point** (`source_totals`) answers "is this point covered by the suite", which is how a closure target is normally stated. A point counts as covered when any elaboration hit it.

Both figures use one unit at every scope, so a test's figure compares with its file's and the run's. A line point is one block of code, not one source line: an `if` arm and its `else` on one line are two points, and the per-elaboration line count equals the `t=line` records in the merged `coverage.dat`. `rb cov summary` shows both figures, and `--by-source` shows the coldest files' source-point figures in the same order. `--coverage-dir-summary` is per elaboration only, because LCOV has already folded elaborations together.

Source points come from the coverage model. A run with only LCOV fallback records no module, so its two figures are equal. If the run produced no model, the summary prints `Coverage source points: unavailable (no coverage model)`.

## Inspect cover-property hits

For Verilator, machine output lists each labeled user cover point as `{name, file, line, module, hits}` on the test result and in the run-level aggregate. The data comes from the per-test `coverage.dat`; no merge flag is needed. Hits are combined across tests by file, line, name and module, so the same included property compiled into different modules stays separate.

Other simulator families omit the field. Omitted means not collected, not zero coverage.

## Use saved coverage artefacts

Every coverage run writes `<command root>/cov_dir/manifest.json`, even without merging, and `cov_dir/coverage-model.json` with the per-file, per-module and per-point detail. `rb cov` and the hub read these; you do not need to open them.

Toggle, expression and labeled cover detail need raw Verilator databases. Without them the model falls back to LCOV and holds only unnamed line and branch data.

## Skip the model when nothing will read it

Per-test attribution grows with points times tests, and for a large toggle-instrumented suite it can dominate the run's output and post-dispatch time. `--coverage-model` on `test` and `regression` chooses how much to write:

- `full` (default): every point with per-test hit counts.
- `totals`: every point and hit count, without per-test attribution.
- `none`: no `coverage-model.json`; a model left by an earlier run is removed.

Console summaries, merges and the directory and source summaries are unchanged. Use `none` for a CI job that records the suite figure and discards its artefacts:

```bash
rb -M cov regression --coverage-merge --coverage-model none
```

`rb cov` and the hub `/cov` pane need a model. Under `totals` they show points and totals without attribution; under `none` they exit with an error.

## Query saved coverage with `rb cov`

`rb cov` reads existing artefacts and writes nothing. Without `--cov-dir` it uses the newest `cov_dir/manifest.json` under the project root.

```bash
rb cov summary
rb cov summary --limit 0
rb cov summary --by-source
rb cov summary --cov-dir verif/blk/cov_dir
rb cov module blk
rb cov module blk --all
```

- `summary` reports run and test totals and the coldest files. `--limit 0` shows all files. `--by-source` shows the same files with source-point figures.
- `module` reports the points of exactly the named module, and `--all` includes hit points as well as misses. Module figures are per elaboration.

An unknown module exits 2 and lists close candidates. With `--machine`, both totals blocks are always present. `rb mcp` exposes the same data as `cov_summary` and `cov_module`, with no hub needed; see [The MCP server](graph.md#the-mcp-server).

## Inspect coverage in the hub

```bash
rb hub start --serve-viewer
```

Open `/cov`. The pane shows totals, ranked files, source annotations, points and per-test attribution from the same model as `rb cov`. Its figures are per elaboration by default; the `figures` picker, or `/cov?by=source`, switches them to source points. Line selections focus the source and schematic views, and module selections focus the graph. See [Coverage pane](hub.md#coverage-pane).

After `rb graph results`, the design graph also joins declared `covers:` relationships to observed coverage; see [Coverage on the graph](graph.md#coverage-on-the-graph).

## Troubleshooting

- Exit 2 with a configuration error after adding a coverage flag: no non-skipped test would produce raw coverage. Check that `-M cov` is set and at least one test runs. A selection of only skipped tests is not an error.
- `Coverage merge FAILED` or `FAIL` in the summary: see [Read a failed merge](#read-a-failed-merge).
- No HTML: set `use-lcov: true`, install `genhtml`, and do not use `--coverage-merge-info-process`.
- `Coverage source points: unavailable (no coverage model)`, or `rb cov` reporting `--coverage-model none`: the run wrote no model. Rerun with `--coverage-model full` or `totals`.
- `rb cov module` exits 2: the module is not recorded. Use one of the listed candidates.
