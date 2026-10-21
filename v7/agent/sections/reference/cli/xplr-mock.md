## xplr mock

```text
Usage: rtl-buddy xplr mock [OPTIONS] COMMAND [ARGS]...

 synthetic DSE backend with known optima (dev/CI harness)

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ info   list scenarios: knob specs, metric_meta, cost model, and the analytic ground  │
│        truth (optimum / Pareto front)                                                │
│ run    evaluate one knob vector; with --register, record it as a ledger experiment   │
│        with the outcome attached in one step                                         │
│ score  score the ledger's mockflow experiments against the ground truth: regret      │
│        (single-objective) or hypervolume + distance-to-front (multi-objective)       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
