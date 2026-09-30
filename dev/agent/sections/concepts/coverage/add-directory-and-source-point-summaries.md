## Add directory and source-point summaries

`--coverage-dir-summary` adds rollups for repo-relative directory prefixes. Repeat the flag, or list one prefix per line in a file:

```bash
rb -M cov regression --coverage-merge \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem

rb -M cov regression --coverage-merge \
  --coverage-dir-summary-file coverage_dirs.txt
```

`--coverage-source-summary` adds source-point figures, which differ from the default per-elaboration ones (see [Per-elaboration vs source-point figures](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/#per-elaboration-vs-source-point-figures)):

```bash
rb -M cov regression --coverage-merge --coverage-source-summary
```
