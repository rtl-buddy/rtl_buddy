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

HTML needs `use-lcov: true` and `genhtml` (diagnose with `rb tool-check --explain lcov`) and is written to `coverage_merge.html` under the command root. Coverview is an archive export for CI or handoff and needs the external `info-process` and compatible Coverview tooling. For interactive inspection use `rb cov` or the hub. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/) for all options.
