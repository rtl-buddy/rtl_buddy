## Read the ledger

Use machine mode so each result is one stable JSON envelope:

```bash
rb --machine xplr list
rb --machine xplr show exp-0003
rb --machine xplr frontier
rb --machine xplr diff exp-0002 exp-0003
rb --machine xplr knob-effect fifo_depth
```

- `list` returns compact experiment summaries.
- `show` returns the complete record, including the reproducible `config_snapshot`.
- `frontier` separates non-dominated, dominated, infeasible and excluded experiments. `--metrics name:min,...` overrides directions; `--prefer` sorts the frontier without dropping points.
- `diff` compares knob manifests, direction-aware outcome deltas and pinned Git revisions. `--patch` adds the source diff.
- `knob-effect` reports every declared change to one knob with its metric delta from the parent. An unknown knob returns an empty list plus known names and suggestions.

An empty frontier with a populated `excluded` list usually means the successful experiments lack directed metrics. Fix `metric_meta` before drawing conclusions.
