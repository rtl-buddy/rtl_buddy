## Interpret results

Each mutant has one outcome:

- `KILLED`: an oracle caught the change.
- `SURVIVED`: every oracle passed. Inspect these verification gaps first.
- `ERRORED`: the mutant did not elaborate or compile, or the proof ended without a verdict (an sby error, a timeout, or an `UNKNOWN` other than a failed induction step). It is excluded from scoring.

```text
mutation score = killed / (killed + survived)
```

The score is `n/a` when nothing is scorable. Surviving mutants whose operator predicted observable signal changes are also reported as predicted-observable misses.

The report is `<mut.yaml dir>/artefacts/mut/<campaign>/mut_report.json`, with the baseline verdict, totals, score, and each mutant's operator, outcome, verdict, diff summary and predicted signals. A user mutant's `file` lists the model-relative files its diff touched. Under `--machine`, `mut list` returns `sites`; `mut run` and `mut score` return `report`.
