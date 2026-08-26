## Filter by regression level

Tests with `reglvl` in the selected inclusive range run; others report `SKIP`:

```bash
rb regression --reg-level 2000
rb regression --start-level 1000 --reg-level 3000
```

The default upper level is 0, so an unqualified regression runs must-run tests with `reglvl: 0`. A test may define one level or builder-specific levels. See [Tests](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#filter-by-regression-level).
