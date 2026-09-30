## Interpret results

Each mutant has one outcome:

- `KILLED`: an oracle caught the change.
- `SURVIVED`: every oracle passed. Inspect these verification gaps first.
- `ERRORED`: the mutant did not elaborate or compile. It is excluded from scoring.

```text
mutation score = killed / (killed + survived)
```

The score is `n/a` when nothing is scorable. Surviving mutants whose operator predicted observable signal changes are also reported as predicted-observable misses.

The report is `<mut.yaml dir>/artefacts/mut/<campaign>/mut_report.json`, with the baseline verdict, totals, score, and each mutant's operator, outcome, verdict, diff summary and predicted signals. Under `--machine`, `mut list` returns `sites`; `mut run` and `mut score` return `report`.
