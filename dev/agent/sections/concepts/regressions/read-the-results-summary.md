## Read the results summary

A regression prints a summary to stderr: one row per test, a metadata footer, and a tally of every verdict in the run, in the order `PASS`, `FAIL`, `XFAIL`, `XPASS`, `SKIP`, `NA`:

```text
Results: 780 PASS, 3 FAIL, 2 SKIP (785 total)
```

On a large regression, show only rows that need attention:

```bash
rb --print-failures-only regression -c regression.yaml
```

The flag drops `PASS`, `SKIP` and `XFAIL` rows from the console and keeps the tally. `rtl_buddy.log` and the machine-mode `summary` event still carry every row.
