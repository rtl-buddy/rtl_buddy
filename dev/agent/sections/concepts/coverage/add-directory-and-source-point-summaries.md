## Add directory and source-point summaries

`--coverage-dir-summary` adds rollups for repo-relative directory prefixes. Repeat the flag, or list one prefix per line in a file. No merge flag is needed:

```bash
rb -M cov regression \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem

rb -M cov regression --coverage-dir-summary-file coverage_dirs.txt
```

Each prefix sums the coverage model's source points (`source_totals`) over the files under it, so the rollup agrees with `coverage-model.json` and `rb cov summary --by-source`. The console prints `Coverage <prefix>: L:… B:… T:… F:…`, where `F` is cover points. In machine output, each `payload.coverage.dir_summary` entry has those ratios as `line`, `branch`, `toggle` and `functional`, and `source_totals` with `found`, `hit` and `ratio` per metric, so disjoint prefixes can be summed. A metric with no points under the prefix, such as line coverage of a directory holding only an interface, is null and prints `UNSP`. If the run produced no model, the summary prints `Coverage directory summary: unavailable (no coverage model)`.

`--coverage-source-summary` adds source-point figures, which differ from the default per-elaboration ones (see [Per-elaboration vs source-point figures](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/#per-elaboration-vs-source-point-figures)):

```bash
rb -M cov regression --coverage-merge --coverage-source-summary
```
