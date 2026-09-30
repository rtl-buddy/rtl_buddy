## Run the experiment lifecycle

Each experiment has one registration and one terminal outcome:

```bash
rb --machine xplr register --json manifest.json
# Run rb synth, rb fpga, a vendor flow, or another evaluator.
rb --machine xplr attach-outcome exp-0001 --json outcome.json
```

- `register` allocates an `exp-NNNN` id, pins the source, records the declared changes and writes `outcome.status: pending`.
- `attach-outcome` accepts `success` or `failed`. Replacing a terminal outcome needs `--force`.
- Use `failed` only when the flow did not complete. A completed but infeasible point is `success` with `routed: false`, which keeps it off the Pareto frontier and preserves its measurements.
- Records live at `<project root>/artefacts/xplr/<id>/record.json` whatever directory you run from. Ledger writes have their own lock and do not contend with suite artefact locks.
