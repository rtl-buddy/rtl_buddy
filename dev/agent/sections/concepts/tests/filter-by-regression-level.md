## Filter by regression level

A test's `reglvl` may be one integer or a builder-specific mapping:

```yaml
reglvl:
  default: 2500
  vcs: 3500
```

Filter a single suite with:

```bash
rb test --reg-level 2000
rb test --start-level 1000 --reg-level 3000
```

The range is inclusive. Tests outside it report `SKIP`. With `rb test`, omitting both flags runs every test regardless of level. An unqualified [regression](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/regressions/#filter-by-regression-level) instead defaults to level 0.
