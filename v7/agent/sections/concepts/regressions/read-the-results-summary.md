## Read the results summary

A regression prints test rows, metadata, and a verdict tally to stderr:

```text
Results: 780 PASS, 3 FAIL, 2 SKIP (785 total)
```

To hide `PASS`, `SKIP`, and `XFAIL` rows:

```bash
rb --print-failures-only regression -c regression.yaml
```

Machine mode filters by default. The tally always counts every verdict; stdout results and the log's `summary` event retain all rows. See [Machine mode](https://rtl-buddy.github.io/rtl_buddy/v7/agents/#machine-mode).
