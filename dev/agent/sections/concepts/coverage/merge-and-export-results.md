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

`rb randtest` takes the same flags and runs the same merge, model and manifest over its seeds. Each seed is one coverage test named like its artefact directory, `<test>/run-NNNN`, so per-test exports and attribution stay apart. `--coverage-per-test` is regression-only, because it packages one Coverview dataset per test across a regression's suites; `randtest` does not accept it. A replay (`-r`) runs the tail in-process over its one seed, rewriting `cov_dir/manifest.json` and `coverage-model.json` for that seed alone, and never checks for an earlier run's coverage job (see [Interrupted runs](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#interrupted-runs-warn-cancel-adopt)).

```bash
rb -M cov randtest basic 50 --coverage-merge
```

HTML needs `use-lcov: true` and `genhtml` (diagnose with `rb tool-check --explain lcov`) and is written to `coverage_merge.html` under the command root. Coverview is an archive export for CI or handoff and needs the external `info-process` and compatible Coverview tooling. For interactive inspection use `rb cov` or the hub. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/) for all options.
