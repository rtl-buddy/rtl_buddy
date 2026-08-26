## xplr frontier

```text
Usage: rtl-buddy xplr frontier [OPTIONS]

 curate the Pareto frontier (non-dominated set) over the declared numeric outcome
 metrics; dominated, infeasible (routed=false), and excluded experiments are reported
 alongside

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --metrics        TEXT  override/declare dominance directions: 'name:min,name2:max'   │
│                        (record-level metric_meta otherwise)                          │
│ --prefer         TEXT  scalar preference to sort the frontier (never drops           │
│                        non-dominated points): comma/plus-separated weight*metric,    │
│                        e.g. '0.7*lut_pct+0.3*delay_ns'; lower score = better after   │
│                        direction normalization                                       │
│ --help                 Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
