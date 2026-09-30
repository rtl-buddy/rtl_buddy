## Filter by regression level

Tests whose `reglvl` falls in the inclusive range run; the rest report `SKIP`:

```bash
rb regression --reg-level 2000
rb regression --start-level 1000 --reg-level 3000
```

The default upper level is 0, so an unqualified regression runs only tests with `reglvl: 0`. A test may set one level or builder-specific levels; see [Tests](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#filter-by-regression-level).
