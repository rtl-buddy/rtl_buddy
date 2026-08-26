## Generating merged output

### LCOV HTML report

Requires `use-lcov: true` in `cfg-coverage`. Not supported with `--coverage-merge-info-process`.

```bash
rtl-buddy --builder-mode cov regression --coverage-merge --coverage-html
```

Output is written to `coverage_merge.html` in the current directory.

### Coverview zip

```bash
rtl-buddy --builder-mode cov regression --coverage-merge --coverage-coverview
```

In regression mode, use `--coverage-per-test` to package one Coverview dataset per test instead of merging:

```bash
rtl-buddy --builder-mode cov regression --coverage-coverview --coverage-per-test
```
