## Results table

A summary table prints after each run, with one row per analysis:

```
                       CDC Results Summary
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ CDC Analysis    ┃ Result ┃ Violations ┃ Suppressed ┃ Crossings ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ demo_cdc_full   │ PASS   │ 0          │ 3          │ 142       │
└─────────────────┴────────┴────────────┴────────────┴───────────┘
```

- **Violations** — non-waived findings parsed from `summary.violations` in the JSON report.
- **Suppressed** — waiver-matched findings, parsed from `summary.suppressed`.
- **Crossings** — total clock-domain crossings detected (informational, parsed from `summary.crossings`).
