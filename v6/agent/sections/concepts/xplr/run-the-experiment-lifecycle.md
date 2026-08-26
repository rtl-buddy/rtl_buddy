## Run the experiment lifecycle

Each experiment has one registration and one terminal outcome:

```bash
rb --machine xplr register --json manifest.json
# Run rb synth, rb fpga, a vendor flow, or another evaluator.
rb --machine xplr attach-outcome exp-0001 --json outcome.json
```

`register` allocates an `exp-NNNN` id, pins the source, records the declared changes, and writes `outcome.status: pending`. `attach-outcome` accepts `success` or `failed`; replacing a terminal outcome requires `--force`.

Use `failed` only when the flow did not complete. A completed but infeasible design point is `success` with `routed: false`, which keeps it out of the Pareto frontier without losing its measurements.

Records live at `<project root>/artefacts/xplr/<id>/record.json` regardless of the invocation directory. Ledger writes use their own lock and do not contend with suite artefact locks.
