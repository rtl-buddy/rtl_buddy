## Results table

A summary table prints after each run, with one row per analysis:

```
                                      CDC Lint Results Summary
┏━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ CDC Analysis  ┃ Result ┃ Description                       ┃ Violations ┃ Suppressed ┃ Crossings ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ demo_cdc_full │ PASS   │ no rule violations (3 suppressed) │ 0          │ 3          │ 142       │
└───────────────┴────────┴───────────────────────────────────┴────────────┴────────────┴───────────┘
```

- **Description** — short human-readable status (`no rule violations`, `no rule violations (N suppressed)`, or the failure reason for FAIL/SKIP rows).
- **Violations** — non-waived findings parsed from `summary.violations` in the JSON report.
- **Suppressed** — waiver-matched findings, parsed from `summary.suppressed`.
- **Crossings** — total clock-domain crossings detected (informational, parsed from `summary.crossings`).

`rb cdc-regression` prints the same columns under the title **CDC Regression Summary** with a `Reg Level: N` metadata line.

Pass `--machine` (a global flag, before the subcommand: `rb --machine cdc`) to get a JSON envelope on stdout instead of the table. Each result row carries `name`, `result`, `desc`, and — when available — `violations`, `suppressed`, and `crossings`; `cdc-regression` rows additionally carry `suite`. `rb --machine cdc --list` emits a `names` array.
