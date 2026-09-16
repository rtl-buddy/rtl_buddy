## Read the results summary

A regression prints a summary to stderr: one row per test, the metadata footer, and a tally of every verdict in the run.

```text
Results: 780 PASS, 3 FAIL, 2 SKIP (785 total)
```

Verdicts are listed in the order `PASS`, `FAIL`, `XFAIL`, `XPASS`, `SKIP`, `NA`, and the tally always counts the whole run.

On a large regression, show only the rows that need attention:

```bash
rb --print-failures-only regression -c regression.yaml
```

The flag drops `PASS`, `SKIP`, and `XFAIL` rows from the console render and keeps the tally. `rtl_buddy.log` and the machine-mode `summary` event still carry every row, so saved records and downstream parsing see the full result set.
