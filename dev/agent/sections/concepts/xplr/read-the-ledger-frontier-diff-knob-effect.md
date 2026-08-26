## Read the ledger: frontier, diff, knob-effect

Use machine mode so the result is a single stable JSON envelope:

```bash
rb --machine xplr list
rb --machine xplr show exp-0003
rb --machine xplr frontier
rb --machine xplr diff exp-0002 exp-0003
rb --machine xplr knob-effect fifo_depth
```

- `list` returns compact experiment summaries.
- `show` returns the complete record, including the reproducible `config_snapshot`.
- `frontier` separates non-dominated, dominated, infeasible, and excluded experiments. Use `--metrics name:min,...` to override directions and `--prefer` to sort the frontier without dropping points.
- `diff` compares knob manifests, direction-aware outcome deltas, and pinned Git revisions. Add `--patch` for the source diff.
- `knob-effect` reports each declared change to one knob and its metric delta from the parent. An unknown knob returns an empty effect list plus known names and suggestions.

An empty frontier with populated `excluded` usually means successful experiments lack directed metrics. Fix `metric_meta` before drawing optimization conclusions.
