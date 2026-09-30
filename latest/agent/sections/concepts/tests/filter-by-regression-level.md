## Filter by regression level

A test's `reglvl` is one integer or a per-builder mapping:

```yaml
reglvl:
  default: 2500
  vcs: 3500
```

```bash
rb test --reg-level 2000
rb test --start-level 1000 --reg-level 3000
```

The range is inclusive, and tests outside it report `SKIP`. Without either flag `rb test` runs every test. An unqualified [regression](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/regressions/#filter-by-regression-level) defaults to level 0.
