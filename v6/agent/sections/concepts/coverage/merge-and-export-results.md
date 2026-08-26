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

Add directory rollups with repeatable repo-relative prefixes or a file containing one prefix per line:

```bash
rb -M cov regression --coverage-merge \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem

rb -M cov regression --coverage-merge \
  --coverage-dir-summary-file coverage_dirs.txt
```

See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/) for the complete option set.
