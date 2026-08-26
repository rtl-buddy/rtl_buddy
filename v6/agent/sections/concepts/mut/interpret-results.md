## Interpret results

Each mutant has one outcome:

- `KILLED`: an oracle caught the change.
- `SURVIVED`: all oracles passed; inspect this verification gap first.
- `ERRORED`: the mutant could not elaborate or compile and is excluded from scoring.

```text
mutation score = killed / (killed + survived)
```

If nothing is scorable, the score is `n/a`. Surviving mutants whose operator predicted observable signal changes are also reported as predicted-observable misses.

The report is `<mut.yaml dir>/artefacts/mut/<campaign>/mut_report.json`. It records the baseline verdict, totals, score, and each mutant's operator, outcome, verdict, diff summary, and predicted signals.

With the global `--machine` flag, `mut list` returns `sites`; `mut run` and `mut score` return `report`.

`mut run` exits 0 when any result is scorable and 1 when the score is `n/a`. It does not gate on a score threshold. `mut list` and `mut score` exit 0 on success; configuration, engine, and report errors are fatal.
